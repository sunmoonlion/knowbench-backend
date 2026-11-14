"""Real storage; synthetic receipts test admission, never claim runtime validation."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_capabilities_db import OPERATION, body, service
from test_workbench_ledger_db import OTHER, OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_methods import FormalMethods
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import Forbidden, NotFound
from app.domain.workbench.formal_method_admission_generated import (
    CONTRACT,
    GOVERNANCE_CHECKS,
    FormalMethodSource,
    FormalMethodValidationReceipt,
    FormalMethodValidationRequest,
    FrozenFormalMethod,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


def validation_request(source):
    slot = {
        "description": "Message",
        "schema": {"type": "string"},
        "required": True,
        "meaning": {},
        "on_missing": "pause",
    }
    return FormalMethodValidationRequest.model_validate(
        {
            "contract": CONTRACT,
            "run_id": str(uuid4()),
            "owner_subject": "author",
            "request_id": str(uuid4()),
            "workdraft_id": str(uuid4()),
            "revision": 1,
            "method_id": str(uuid4()),
            "dependencies": [],
            "draft": {
                "source": source,
                "allowed_callers": ["host"],
                "definition": {
                    "contract": "knowlab/formal-method/v1",
                    "capability_declaration": {
                        "title": "Synthetic notice",
                        "purpose": "Return one receipt",
                        "applies_when": ["A test recipient exists"],
                        "excludes": [],
                        "promises": {"sent": "Return a receipt"},
                    },
                    "access_requirements": [],
                    "procedure": {
                        "kind": "direct",
                        "implementation": {
                            **{
                                k: source[k]
                                for k in (
                                    "kind",
                                    "source",
                                    "version",
                                    "digest",
                                    "entry",
                                )
                            },
                            "requirement_ids": [],
                        },
                        "inputs": {"to": slot},
                        "outputs": {"receipt": slot},
                        "instructions": "Send only to the approved recipient",
                        "stop_conditions": ["A receipt or error"],
                        "max_model_turns": 0,
                    },
                    "how_to_validate": [
                        {
                            "promise_id": "sent",
                            "outputs": ["receipt"],
                            "check": "Check receipt",
                            "evidence": "Recorded result",
                            "on_failure": "fail",
                        }
                    ],
                    "what_requires_approval": [],
                },
            },
        }
    )


async def setup(db, *, licensed=False):
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await s.execute(
                text("""INSERT INTO auth_user(id,issuer,subject,roles,scopes)
                VALUES (:o,'test','author','[]','[]'),(:other,'test','other','[]','[]')"""),
                {"o": OWNER, "other": OTHER},
            )
        asset = await service(repo).create_connection(OWNER, body())
        if licensed:
            async with repo.transaction():
                # Fixture represents operator-verified evidence, not a UI claim.
                await s.execute(
                    text("""UPDATE workbench_capability_assets SET detail=
                    detail || '{"execution_license_basis":"synthetic verified permission"}'::jsonb WHERE id=:id"""),
                    {"id": asset["id"]},
                )
        catalog = await FormalMethods(repo, operations=(OPERATION,)).sources("author")
        assert len(catalog["items"]) == 1
        return validation_request(catalog["items"][0]), asset


async def test_intake_persists_missing_governance_without_fabricating_pass(db):
    request, asset = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = FormalMethods(repo, operations=(OPERATION,))
        result = await svc.validate(request, caller="knowlab")
        assert result["status"] == "pending" and result["receipt"] is None
        assert set(result["missing_checks"]) == {
            "source",
            "authorization",
            "approval",
            "isolation",
            "outcome",
        }
        assert await svc.validate(request, caller="knowlab") == result
        assert (
            await svc.validation("author", str(request.run_id), caller="knowlab")
            == result
        )
        with pytest.raises(NotFound):
            await svc.validation("other", str(request.run_id), caller="knowlab")
        with pytest.raises(NotFound):
            await svc.validation("author", str(request.run_id), caller="other")
        with pytest.raises(CapabilityError):
            await svc.validate(
                request.model_copy(update={"revision": 2}), caller="knowlab"
            )
        # request_id identifies the author project, not an idempotency token.
        # A later revision must be able to start a fresh validation run.
        updated = request.model_copy(update={"run_id": uuid4(), "revision": 2})
        assert (await svc.validate(updated, caller="knowlab"))["run_id"] == str(
            updated.run_id
        )
        unchanged = await repo.capability_asset(OWNER, asset["id"])
        assert unchanged["status"] == "active" and unchanged["revision"] == 1


async def test_sources_are_owner_scoped_reviewed_and_pinned(db):
    request, asset = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = FormalMethods(repo, operations=(OPERATION,))
        assert await svc.sources("other") == {"items": []}
        assert await FormalMethods(repo).sources("author") == {"items": []}
        public = await svc.sources("author")
        assert "only-server-knows" not in json.dumps(public)
        with pytest.raises(NotFound):
            await svc.validate(
                request.model_copy(update={"owner_subject": "other"}), caller="knowlab"
            )
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                ),
                {"id": asset["id"]},
            )
        with pytest.raises(CapabilityError, match="changed"):
            await svc.validate(request, caller="knowlab")


async def test_concurrent_intake_uses_one_run_and_does_not_reinstall(db):
    request, _ = await setup(db)

    async def accept():
        async with db() as s:
            return await FormalMethods(
                WorkbenchRepository(s), operations=(OPERATION,)
            ).validate(request, caller="knowlab")

    first, second = await asyncio.gather(accept(), accept())
    assert first == second
    async with db() as s:
        assert (
            await s.execute(text("SELECT count(*) FROM workbench_formal_validations"))
        ).scalar() == 1
        assert (
            await s.execute(text("SELECT count(*) FROM workbench_capability_assets"))
        ).scalar() == 1


def frozen(request):
    source = request.draft.source
    receipt = FormalMethodValidationReceipt.model_validate(
        {
            "contract": CONTRACT,
            "run_id": request.run_id,
            "owner_subject": request.owner_subject,
            "request_id": request.request_id,
            "workdraft_id": request.workdraft_id,
            "revision": request.revision,
            "method_id": request.method_id,
            "definition_digest": request.draft.definition.digest(),
            "source_digest": source.fingerprint() if source else None,
            "dependencies": [],
            "checks": sorted(GOVERNANCE_CHECKS | {"business"}),
            "uncovered": [],
            "completed_at": datetime.now(UTC),
        }
    )
    return FrozenFormalMethod.model_validate(
        {
            "contract": "knowlab/frozen-formal-method/v1",
            "owner_subject": request.owner_subject,
            "reference": {
                "method_id": request.method_id,
                "version": 1,
                "digest": request.draft.definition.digest(),
            },
            "draft": request.draft,
            "validation": receipt,
            "visibility": "private",
        }
    )


async def test_forged_receipt_rejected_and_trusted_fixture_registers_idempotently(db):
    request, asset = await setup(db, licensed=True)
    version = frozen(request)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = FormalMethods(repo, operations=(OPERATION,))
        pending = await svc.validate(request, caller="knowlab")
        with pytest.raises(Forbidden):
            await svc.register(version, caller="knowlab")
        async with repo.transaction():
            # Seed a synthetic trusted runtime receipt to test persistence guards.
            # There is intentionally no HTTP endpoint to upload a passed result.
            await s.execute(
                text(
                    "UPDATE workbench_formal_validations SET result=cast(:result as jsonb) WHERE run_id=:id"
                ),
                {
                    "id": str(request.run_id),
                    "result": json.dumps(
                        {
                            **pending,
                            "status": "passed",
                            "receipt": version.validation.model_dump(mode="json"),
                            "missing_checks": [],
                            "details": [],
                        }
                    ),
                },
            )
        changed = version.model_copy(
            update={
                "draft": version.draft.model_copy(
                    update={"allowed_callers": ("host", "model")}
                )
            }
        )
        with pytest.raises(Forbidden):
            await svc.register(changed, caller="knowlab")
        registered = await svc.register(version, caller="knowlab")
        assert (
            await svc.registration(
                "author", str(request.method_id), 1, caller="knowlab"
            )
            == registered
        )
        with pytest.raises(NotFound):
            await svc.registration("other", str(request.method_id), 1, caller="knowlab")
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                ),
                {"id": asset["id"]},
            )
        # Revocation affects present execution, never rewrites historical admission.
        assert await svc.register(version, caller="knowlab") == registered
        with pytest.raises(Forbidden):
            await svc.register(changed, caller="knowlab")


async def test_author_license_text_cannot_create_trusted_source_rights(db):
    request, _ = await setup(db)
    source = request.draft.source
    assert source is not None
    forged = FormalMethodSource.model_validate(
        {**source.model_dump(), "license_basis": "I claim permission"}
    )
    changed = request.model_copy(
        update={"draft": request.draft.model_copy(update={"source": forged})}
    )
    async with db() as s:
        with pytest.raises(CapabilityError):
            await FormalMethods(
                WorkbenchRepository(s), operations=(OPERATION,)
            ).validate(changed, caller="knowlab")
