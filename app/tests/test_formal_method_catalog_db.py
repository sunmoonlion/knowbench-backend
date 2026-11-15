"""Resolve retained real admission; neither registration nor old grants suffice."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_capabilities_db import OWNER
from test_formal_execution_db import approve, setup, status
from test_formal_host_calls_db import FORMAL_OPERATION, host_service
from test_formal_method_admission_db import frozen as synthetic_frozen
from test_source_licenses import license_record
from test_workbench_ledger_db import OTHER, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_test_runs import FormalExecutions
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import NotFound
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.infrastructure.workbench.repository import WorkbenchRepository


async def admitted(db, *, cases=None):
    request, asset, remote, factory, conductor = await setup(db, cases=cases)
    for _ in range(20):
        await conductor.advance(str(request.run_id))
        result = await status(db, request, factory)
        if result["status"] in ("passed", "failed", "cancelled"):
            break
        if result["actions"]:
            action = result["actions"][0]
            case = next(
                c
                for c in request.execution.suite.cases
                if c.case_id == action["case_id"]
            )
            decision = (
                "decline"
                if case.expected.rejection_code == "approval_denied"
                else "accept"
            )
            await approve(db, request, factory, result, decision=decision)
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
    async with db() as session:
        repo = WorkbenchRepository(session)
        await FormalExecutions(repo, factory(repo)).formal.register(
            frozen, caller="knowlab"
        )
    return frozen, request, asset, factory, remote


def catalog(repo, factory):
    capabilities = factory(repo)
    return FormalMethodCatalog(
        repo, capabilities.formal_sources, capabilities.operations
    )


async def test_real_admission_resolves_after_cleanup_and_directory_is_owner_private(db):
    frozen, request, asset, factory, remote = await admitted(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        service = catalog(repo, factory)
        resolved = await service.resolve_owned(OWNER, frozen.reference)
        assert resolved.frozen == frozen
        assert resolved.asset["id"] == asset["id"]
        assert resolved.policy == FORMAL_OPERATION
        assert resolved.uncovered
        items = await service.list_owned(OWNER)
        assert len(items) == 1 and items[0]["available"] and items[0]["issue"] is None
        assert items[0]["reference"] == frozen.reference.model_dump(mode="json")
        assert items[0]["inputs"]["to"]["schema"] == {"type": "string"}
        assert "schema_" not in json.dumps(items)
        assert await service.list_owned(OTHER) == []
        with pytest.raises(NotFound):
            await service.resolve_owned(OTHER, frozen.reference)
        async with repo.transaction():
            await repo.purge_test_run(str(request.run_id))
        assert (await service.resolve_owned(OWNER, frozen.reference)).frozen == frozen
        assert len(remote.calls) == 1  # Resolving never dispatches or spends.


@pytest.mark.parametrize("mutation", ["evidence", "request", "receipt", "missing"])
async def test_changed_or_missing_retained_proof_blocks_invocation(db, mutation):
    frozen, request, _, factory, _ = await admitted(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            row = await repo.formal_validation(OWNER, str(request.run_id))
            if mutation == "request":
                run = await repo.get_test_run(str(request.run_id))
                changed = deepcopy(run["request"])
                changed["execution"]["suite"]["cases"][0]["input"]["values"]["to"] = (
                    "other"
                )
                query = "UPDATE workbench_test_runs SET request=cast(:doc as jsonb) WHERE run_id=:id"
            elif mutation == "receipt":
                changed = deepcopy(row["result"])
                changed["receipt"]["checks"] = []
                query = "UPDATE workbench_formal_validations SET result=cast(:doc as jsonb) WHERE run_id=:id"
            else:
                changed = deepcopy(row["execution_evidence"])
                if mutation == "missing":
                    changed = None
                else:
                    changed["cases"][0]["governance"]["approved"] = False
                query = "UPDATE workbench_formal_validations SET execution_evidence=cast(:doc as jsonb) WHERE run_id=:id"
            await session.execute(
                text(query), {"doc": json.dumps(changed), "id": str(request.run_id)}
            )
        with pytest.raises(CapabilityError):
            await catalog(repo, factory).resolve_owned(OWNER, frozen.reference)
        item = (await catalog(repo, factory).list_owned(OWNER))[0]
        assert not item["available"] and item["issue"]


@pytest.mark.parametrize("change", ["source", "license", "cost", "policy"])
async def test_current_rights_or_policy_change_does_not_reuse_historical_grants(
    db, change
):
    frozen, _, asset, factory, remote = await admitted(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        if change == "source":
            async with repo.transaction():
                await session.execute(
                    text(
                        "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                    ),
                    {"id": asset["id"]},
                )
            service = catalog(repo, factory)
        else:
            record = license_record(frozen.draft.source.model_dump())
            operation = FORMAL_OPERATION
            if change == "license":
                record = record.model_copy(update={"status": "revoked"})
            elif change == "cost":
                operation = operation.model_copy(update={"formal_execution_cost": None})
            else:
                cost = operation.formal_execution_cost.model_copy(
                    update={"evidence_digest": "sha256:" + "b" * 64}
                )
                operation = operation.model_copy(update={"formal_execution_cost": cost})
            caps = host_service(repo, record, remote, operations=(operation,))
            service = FormalMethodCatalog(repo, caps.formal_sources, caps.operations)
        with pytest.raises(CapabilityError):
            await service.resolve_owned(OWNER, frozen.reference)
        assert (await service.list_owned(OWNER))[0]["available"] is False


async def test_legacy_fixture_receipt_is_listed_but_never_callable(db):
    frozen, request, _, factory, _ = await admitted(db)
    legacy = synthetic_frozen(request.admission_request())
    async with db() as session:
        repo = WorkbenchRepository(session)
        # Historical register fixtures remain readable, not runtime eligibility.
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_formal_methods SET document=cast(:doc as jsonb) WHERE method_id=:id"
                ),
                {
                    "id": str(request.method_id),
                    "doc": legacy.model_dump_json(by_alias=True),
                },
            )
        with pytest.raises(CapabilityError, match="real execution"):
            await catalog(repo, factory).resolve_owned(OWNER, frozen.reference)
        item = (await catalog(repo, factory).list_owned(OWNER))[0]
        assert not item["available"] and "real execution" in item["issue"]


@pytest.mark.parametrize("rejection", ["input_invalid", "approval_denied"])
async def test_validated_negative_cases_remain_usable_with_real_positive_case(
    db, rejection
):
    cases = [
        {
            "case_id": "negative",
            "input": {
                "values": {}
                if rejection == "input_invalid"
                else {"to": "test@example.test"}
            },
            "repetitions": 1,
            "minimum_passes": 1,
            "expected": {
                "outcome": "rejected",
                "rejection_code": rejection,
                "assertions": [],
            },
        },
        {
            "case_id": "positive",
            "input": {"values": {"to": "test@example.test"}},
            "repetitions": 1,
            "minimum_passes": 1,
            "expected": {
                "outcome": "succeeded",
                "assertions": [
                    {
                        "kind": "equals",
                        "pointer": "/receipt",
                        "value": "expected-never-sent",
                    }
                ],
            },
        },
    ]
    frozen, _, _, factory, remote = await admitted(db, cases=cases)
    async with db() as session:
        assert (
            await catalog(WorkbenchRepository(session), factory).resolve_owned(
                OWNER, frozen.reference
            )
        ).frozen == frozen
    assert len(remote.calls) == 1


async def test_history_includes_only_owned_session_roots_and_applies_limit(db):
    _, _, sid = await seed(db)
    _, _, other_sid = await seed(db, root="/home/u/research/other")
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            roots = []
            for number in range(3):
                call = await repo.add_call(
                    {
                        "id": str(uuid4()),
                        "owner_actor_id": OWNER,
                        "session_id": sid,
                        "kind": "method",
                        "origin": "method",
                        "call_key": "formal-session:" + str(uuid4()),
                        "digest": uuid4().hex + uuid4().hex,
                        "status": "created",
                        "detail": {"number": number},
                    }
                )
                roots.append(call)
                await session.execute(
                    text(
                        "UPDATE workbench_calls SET created_at=now()+make_interval(secs=>:n) WHERE id=:id"
                    ),
                    {"n": number, "id": call["id"]},
                )
            for changes in (
                {"session_id": other_sid},
                {"parent_call_id": roots[0]["id"]},
                {"call_key": "task:" + str(uuid4())},
            ):
                await repo.add_call(
                    {
                        "id": str(uuid4()),
                        "owner_actor_id": OWNER,
                        "session_id": sid,
                        "kind": "method",
                        "origin": "method",
                        "call_key": "formal-session:" + str(uuid4()),
                        "digest": uuid4().hex + uuid4().hex,
                        "status": "created",
                        "detail": {},
                        **changes,
                    }
                )
        history = await repo.list_formal_session_calls(OWNER, sid, limit=2)
        assert [row["id"] for row in history] == [roots[2]["id"], roots[1]["id"]]
        assert all(isinstance(row["id"], str) for row in history)
        assert len(await repo.list_formal_session_calls(OWNER, sid)) == 3
        assert await repo.list_formal_session_calls(OTHER, sid) == []
        with pytest.raises(ValueError):
            await repo.list_formal_session_calls(OWNER, sid, limit=51)
