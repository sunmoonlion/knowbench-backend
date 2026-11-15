"""Caller eligibility from real local model incubation, retained after cleanup."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from formal_model_support import ModelServer, setup
from sqlalchemy import text
from test_capabilities_db import OWNER
from test_formal_host_calls_db import FORMAL_OPERATION, host_service
from test_formal_method_catalog_db import admitted, catalog
from test_formal_model_admission_db import _decide_case
from test_source_licenses import license_record
from test_workbench_advisor_db import close
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_test_runs import FormalExecutions
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import NotFound
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


async def model_admitted(db, *, mixed=False):
    fake, runner = ModelServer(), None
    await fake.start()
    try:
        original, remote, factory, conductor, runner = await setup(db, fake)
        request = original
        if mixed:
            doc = original.model_dump(mode="json", by_alias=True)
            model_case = doc["execution"]["suite"]["cases"][0]
            host = deepcopy(model_case)
            host.update(case_id="host-notice", caller="host")
            host["input"].pop("model_prompt")
            suite = FormalTestSuiteDraft.model_validate(
                {"suite_id": str(uuid4()), "version": 1, "cases": [host, model_case]}
            ).freeze()
            doc["run_id"] = str(uuid4())
            doc["draft"]["allowed_callers"] = ["host", "model"]
            doc["execution"]["suite"] = suite.model_dump(mode="json")
            request = FormalMethodExecutionRequest.model_validate(doc)
            async with db() as session:
                repo = WorkbenchRepository(session)
                await FormalExecutions(repo, factory(repo)).accept(
                    request, caller="knowlab"
                )
        for case in request.execution.suite.cases:
            await _decide_case(
                db, fake, request, case, runner, conductor, decision="accept"
            )
        await conductor.advance(str(request.run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            service = FormalExecutions(repo, factory(repo))
            result = await service.status(
                "author", str(request.run_id), caller="knowlab"
            )
            assert result["status"] == "passed", result
            frozen = FrozenFormalMethod.model_validate(
                {
                    "contract": "knowlab/frozen-formal-method/v1",
                    "owner_subject": request.owner_subject,
                    "reference": {
                        "method_id": request.method_id,
                        "version": 1,
                        "digest": request.draft.definition.digest(),
                    },
                    "draft": request.draft,
                    "validation": result["admission"]["receipt"],
                    "visibility": "private",
                }
            )
            assert (await service.formal.register(frozen, caller="knowlab"))[
                "status"
            ] == "registered"
        assert len(fake.turn_inputs) == 1 and len(remote.calls) == (2 if mixed else 1)
        assert not fake.failures
        return frozen, request, factory, remote
    finally:
        if runner:
            await close(runner)
        await fake.close()


@pytest.mark.parametrize("mixed", [False, True])
async def test_real_model_and_mixed_caller_eligibility_survives_cleanup(db, mixed):
    frozen, request, factory, remote = await model_admitted(db, mixed=mixed)
    async with db() as session:
        repo = WorkbenchRepository(session)
        service = catalog(repo, factory)
        for purge in (False, True):
            if purge:
                async with repo.transaction():
                    await repo.purge_test_run(str(request.run_id))
            assert (
                await service.resolve_owned(OWNER, frozen.reference, caller="model")
            ).frozen == frozen
            item = (await service.list_owned(OWNER, caller="model"))[0]
            assert item["available"] and item["issue"] is None
            assert item["inputs"]["to"]["schema"] == {"type": "string"}
            if mixed:
                assert (
                    await service.resolve_owned(OWNER, frozen.reference)
                ).frozen == frozen
                assert (await service.list_owned(OWNER))[0]["available"]
            else:
                with pytest.raises(CapabilityError):
                    await service.resolve_owned(OWNER, frozen.reference)
                assert not (await service.list_owned(OWNER))[0]["available"]
        assert await service.list_owned(OTHER, caller="model") == []
        with pytest.raises(NotFound):
            await service.resolve_owned(OTHER, frozen.reference, caller="model")
        assert len(remote.calls) == (2 if mixed else 1)


async def test_old_real_host_receipt_never_acquires_model_eligibility(db):
    frozen, _, _, factory, _ = await admitted(db)
    async with db() as session:
        service = catalog(WorkbenchRepository(session), factory)
        with pytest.raises(CapabilityError):
            await service.resolve_owned(OWNER, frozen.reference, caller="model")
        assert not (await service.list_owned(OWNER, caller="model"))[0]["available"]
        assert (await service.list_owned(OWNER))[0]["available"]


async def test_changed_model_coverage_and_sealed_proof_are_unavailable(db):
    frozen, request, factory, _ = await model_admitted(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.formal_validation(OWNER, str(request.run_id))
        assert row is not None
        original = deepcopy(row["execution_evidence"])
        for mutation in ("coverage", "provenance", "cost"):
            changed = deepcopy(original)
            if mutation == "coverage":
                changed["validated_callers"] = ["host"]
            elif mutation == "provenance":
                changed["cases"][0]["model_provenance"]["invocation"]["turn_id"] = (
                    "another"
                )
            else:
                changed["model_cost"] = "0"
            async with repo.transaction():
                await session.execute(
                    text(
                        "UPDATE workbench_formal_validations SET execution_evidence=cast(:doc as jsonb) WHERE run_id=:id"
                    ),
                    {"doc": json.dumps(changed), "id": str(request.run_id)},
                )
            with pytest.raises(CapabilityError):
                await catalog(repo, factory).resolve_owned(
                    OWNER, frozen.reference, caller="model"
                )
            assert not (await catalog(repo, factory).list_owned(OWNER, caller="model"))[
                0
            ]["available"]


async def test_sealed_model_semantics_rechecked_for_every_case(db):
    frozen, request, factory, _ = await model_admitted(db, mixed=True)
    async with db() as session:
        repo = WorkbenchRepository(session)
        service = catalog(repo, factory)
        resolved = await service.resolve_owned(OWNER, frozen.reference, caller="model")
        row = await repo.formal_validation(OWNER, str(request.run_id))
        assert row is not None
        proof = row["execution_evidence"]
        # These in-memory corruptions test the semantic verifier beneath the seal;
        # none is written as a receipt, resealed or submitted as actual admission.
        paths = [
            (("model_cost",), "0"),
            (("cases", 0, "model_turn_ids"), ["model-turn"]),
            (("cases", 0, "governance", "approved"), False),
            (("cases", 1, "snapshot_digest"), "0" * 64),
            (("cases", 1, "result", "model_trace", "method_call_id"), str(uuid4())),
            (("cases", 1, "model_provenance", "validated"), False),
            (("cases", 1, "model_provenance", "invocation", "turn_id"), "wrong"),
            (("cases", 1, "model_provenance", "isolation", "scope_id"), ""),
            (("cases", 1, "model_provenance", "isolation", "selection_revision"), -1),
            (
                ("cases", 1, "model_provenance", "isolation", "snapshot_digest"),
                "0" * 64,
            ),
            (
                (
                    "cases",
                    1,
                    "model_provenance",
                    "isolation",
                    "startup_features",
                    "goals",
                ),
                True,
            ),
            (
                (
                    "cases",
                    1,
                    "model_provenance",
                    "isolation",
                    "inventory",
                    0,
                    "resources",
                ),
                [{"uri": "private"}],
            ),
            (("cases", 1, "model_provenance", "turn_events"), []),
            (("cases", 1, "model_provenance", "call_events", 1, "cursor"), 0),
            (("cases", 1, "model_provenance", "usage", 0, "cost", "priced"), False),
            (("cases", 1, "model_provenance", "budget_consumed", "amount"), "0"),
            (("cases", 1, "model_provenance", "ledger"), []),
            (("cases", 1, "governance", "formal_probe", "attempt_id"), str(uuid4())),
        ]
        for path, value in paths:
            changed = deepcopy(proof)
            target = changed
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            with pytest.raises(CapabilityError):
                service._verify_cases(
                    OWNER,
                    frozen,
                    request,
                    changed,
                    resolved.registration,
                    resolved.policy,
                )


async def test_current_model_source_license_and_cost_policy_remain_required(db):
    frozen, _, factory, remote = await model_admitted(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        source = frozen.draft.source
        assert source is not None
        record = license_record(source.model_dump())
        for change in ("license", "cost", "policy"):
            operation = FORMAL_OPERATION
            license = record
            if change == "license":
                license = record.model_copy(update={"status": "revoked"})
            elif change == "cost":
                operation = operation.model_copy(update={"formal_execution_cost": None})
            else:
                assert operation.formal_execution_cost is not None
                cost = operation.formal_execution_cost.model_copy(
                    update={"evidence_digest": "sha256:" + "b" * 64}
                )
                operation = operation.model_copy(update={"formal_execution_cost": cost})
            caps = host_service(repo, license, remote, operations=(operation,))
            service = FormalMethodCatalog(repo, caps.formal_sources, caps.operations)
            with pytest.raises(CapabilityError):
                await service.resolve_owned(OWNER, frozen.reference, caller="model")
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                ),
                {"id": str(source.asset_id)},
            )
        with pytest.raises(CapabilityError):
            await catalog(repo, factory).resolve_owned(
                OWNER, frozen.reference, caller="model"
            )
