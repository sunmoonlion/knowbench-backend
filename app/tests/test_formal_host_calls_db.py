"""Real ledger, approvals and calls; synthetic MCP only, without any model turn."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from method_access_support import ACCESS
from sqlalchemy import text
from test_capabilities_db import CIPHER, OPERATION, OWNER, Remote, approval
from test_formal_method_admission_db import setup as admission_setup
from test_source_licenses import license_record
from test_workbench_ledger_db import OTHER, handover, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.formal_execution import FormalExecution
from app.application.workbench.formal_methods import FormalMethods
from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.capability_contract import Call
from app.domain.workbench.errors import Forbidden, NotFound
from app.domain.workbench.formal_execution import (
    FORMAL_RUNTIME,
    FormalTaskSnapshot,
    PinnedFormalMethod,
    case_key,
)
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.method_authorization import MethodAuthorization
from app.domain.workbench.states import AttemptState, TaskState
from app.domain.workbench.tool_operations import FormalExecutionCost
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

FORMAL_OPERATION = OPERATION.model_copy(
    update={
        "formal_execution_cost": FormalExecutionCost(
            incremental_cost="0",
            currency="CNY",
            evidence_reference="operator:test-fixture",
            evidence_digest="sha256:" + "a" * 64,
        )
    }
)


def host_service(repo, record, remote, *, operations=(FORMAL_OPERATION,)):
    return Capabilities(
        repo, remote, CIPHER, operations=operations, source_licenses=(record,)
    )


async def setup(db):
    admission, asset = await admission_setup(db, licensed=True)
    env, _, sid = await seed(db)
    body = admission.model_dump(mode="json", by_alias=True)
    body["draft"]["definition"]["access_requirements"] = [deepcopy(ACCESS)]
    body["draft"]["definition"]["procedure"]["implementation"]["requirement_ids"] = [
        ACCESS["requirement_id"]
    ]
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": str(uuid4()),
            "version": 1,
            "cases": [
                {
                    "case_id": "notice",
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
                }
            ],
        }
    ).freeze()
    request = FormalMethodExecutionRequest.model_validate(
        {
            **body,
            "contract": FORMAL_RUNTIME,
            "execution": {
                "suite": suite.model_dump(mode="json"),
                "runner": {
                    "kind": "user",
                    "environment_id": env,
                    "workspace_root": "/home/u/research",
                },
                "budget": {"limit": "1", "currency": "CNY"},
                "timeout_seconds": 60,
                "max_wait_seconds": 120,
            },
        }
    )
    assert request.draft.source is not None
    record = license_record(request.draft.source.model_dump())
    snapshot = FormalTaskSnapshot(
        runtime_contract=FORMAL_RUNTIME,
        run_id=request.run_id,
        case_id="notice",
        repetition=1,
        request_digest=request.fingerprint(),
        method_id=request.method_id,
        draft=request.draft,
        arguments={"to": "test@example.test"},
        definition_digest=request.draft.definition.digest(),
    )
    async with db() as s:
        repo = WorkbenchRepository(s)
        await FormalMethods(
            repo, operations=(OPERATION,), source_licenses=(record,)
        ).validate(request.admission_request(), caller="knowlab")
        async with repo.transaction():
            await repo.insert_test_run(
                {
                    "run_id": str(request.run_id),
                    "caller": "knowlab",
                    "owner_subject": "author",
                    "digest": request.fingerprint().removeprefix("sha256:"),
                    "request": request.model_dump(mode="json", by_alias=True),
                    "budget_limit": "1",
                    "timeout_seconds": 60,
                    "max_wait_seconds": 120,
                },
                [{"case_id": case_key("notice", 1), "position": 0}],
            )
        led = Ledger(repo)
        task_id = (
            await led.handover(
                handover(sid),
                owner_actor_id=OWNER,
                snapshot=PinnedFormalMethod(snapshot, "candidate-1"),
            )
        )["task_id"]
        await led.transition(task_id, TaskState.VALIDATING)
        await led.transition(task_id, TaskState.QUEUED)
        attempt_id = (
            await led.open_attempt(
                task_id,
                step_id="direct",
                step_version="1",
                meta={
                    "kind": "formal_method",
                    "snapshot_digest": digest(snapshot.document()),
                    "slot": "direct",
                },
            )
        )["attempt_id"]
        await led.attempt_transition(attempt_id, AttemptState.RUNNING)
        async with repo.transaction():
            await repo.update_test_case(
                str(request.run_id),
                case_key("notice", 1),
                session_id=sid,
                task_id=task_id,
            )
    return request, asset, record, sid, task_id, attempt_id


async def test_host_uses_real_approval_exact_grant_and_common_adapter_without_model(db):
    _, asset, record, sid, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)
        Call.model_validate(call)
        assert call["status"] == "awaiting_approval" and not remote.calls
        assert call["detail"]["execution_kind"] == "formal_mcp"
        assert (
            not {
                "runtime",
                "selection_revision",
                "method",
                "formal_host",
                "authorization",
            }
            & call["detail"].keys()
        )
        assert "expected-never-sent" not in json.dumps(call["detail"])
        assert await svc.request_host_call(OWNER, task, attempt) == call
        raw = await repo.capability_call(OWNER, call["id"])
        assert raw["task_id"] == task and raw["attempt_id"] == attempt
        assert await repo.method_authorization(OWNER, raw["parent_call_id"]) is None
        assert (await repo.get_task(task))["state"] == TaskState.WAITING
        result = await svc.decide(OWNER, call["id"], approval(call))
        assert result["status"] == "succeeded"
        Call.model_validate(result)
        assert len(remote.calls) == 1 and remote.calls[0][-1] == {
            "to": "test@example.test"
        }
        assert (await repo.get_task(task))["state"] == TaskState.RUNNING
        auth = MethodAuthorization.model_validate(
            await repo.method_authorization(OWNER, raw["parent_call_id"])
        )
        (grant,) = auth.grants
        assert (
            not grant.delegable
            and str(grant.recipient_call_id) == raw["parent_call_id"]
        )
        assert str(grant.connection_id) == asset["id"]
        assert grant.requirement.resources[0].values == ("test@example.test",)
        assert grant.requirement.operations == ("send",)
        assert await svc.decide(OWNER, call["id"], approval(call)) == result
        assert (await svc.request_host_call(OWNER, task, attempt))["id"] == call["id"]
        assert len(remote.calls) == 1
        events = await repo.list_events(session_id=sid)
        assert not any(
            e["type"].startswith(("turn/", "thread/", "item/")) for e in events
        )
        assert any(e["type"] == "method/permissionsConfirmed" for e in events)


async def test_concurrent_host_requests_and_decisions_dispatch_once(db):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()

    async def request():
        async with db() as s:
            return await host_service(
                WorkbenchRepository(s), record, remote
            ).request_host_call(OWNER, task, attempt)

    first, second = await asyncio.gather(request(), request())
    assert first["id"] == second["id"]

    async def decide():
        async with db() as s:
            return await host_service(WorkbenchRepository(s), record, remote).decide(
                OWNER, first["id"], approval(first)
            )

    results = await asyncio.gather(decide(), decide())
    assert all(r["status"] in ("dispatching", "succeeded") for r in results)
    assert len(remote.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "asset",
        "license",
        "policy",
        "scope",
        "arguments",
        "case",
        "owner",
        "attempt",
        "cancel",
        "grant",
        "stored_arguments",
        "cost_evidence",
    ],
)
async def test_host_approval_rechecks_source_policy_and_exact_live_work(db, change):
    _, asset, record, sid, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)
        if change == "license":
            svc = host_service(
                repo, record.model_copy(update={"status": "revoked"}), remote
            )
        elif change == "policy":
            svc = host_service(repo, record, remote, operations=())
        elif change == "cost_evidence":
            svc = host_service(repo, record, remote, operations=(OPERATION,))
        else:
            async with repo.transaction():
                row = await repo.get_task(task)
                if change == "asset":
                    await s.execute(
                        text(
                            "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                        ),
                        {"id": asset["id"]},
                    )
                elif change in ("scope", "arguments"):
                    snapshot = deepcopy(row["pack_snapshot"])
                    if change == "scope":
                        snapshot["draft"]["definition"]["access_requirements"][0][
                            "resources"
                        ][0]["values"] = ["other@example.test"]
                    else:
                        snapshot["arguments"]["to"] = "other@example.test"
                    await s.execute(
                        text(
                            "UPDATE workbench_tasks SET pack_snapshot=cast(:snapshot as jsonb) WHERE id=:id"
                        ),
                        {"snapshot": json.dumps(snapshot), "id": task},
                    )
                elif change == "case":
                    await s.execute(
                        text(
                            "UPDATE workbench_test_cases SET task_id=NULL WHERE task_id=:id"
                        ),
                        {"id": task},
                    )
                elif change == "owner":
                    await s.execute(
                        text("UPDATE workbench_test_runs SET owner_subject='other'")
                    )
                elif change == "attempt":
                    await repo.update_attempt(
                        attempt,
                        refs={
                            "kind": "formal_method",
                            "slot": "direct",
                            "snapshot_digest": "0" * 64,
                        },
                    )
                elif change == "cancel":
                    await Ledger(repo).request_cancel(task, owner_actor_id=OWNER)
                elif change == "stored_arguments":
                    raw = await repo.capability_call(OWNER, call["id"])
                    raw["detail"]["arguments"] = {"to": "other@example.test"}
                    await repo.update_call(
                        OWNER, call["id"], "awaiting_approval", raw["detail"]
                    )
                elif change == "grant":
                    raw = await repo.capability_call(OWNER, call["id"])
                    revoked = MethodAuthorization(
                        call_id=raw["parent_call_id"],
                        revision=1,
                        parent_call_id=None,
                        parent_revision=None,
                        method_id="blocked",
                        revoked=True,
                        expires_at=datetime.now(UTC) + timedelta(minutes=5),
                        grants=(),
                    )
                    await repo.save_method_authorization(
                        OWNER, sid, revoked.model_dump(mode="json"), previous=0
                    )
        with pytest.raises((CapabilityError, Forbidden)):
            await svc.decide(OWNER, call["id"], approval(call))
        assert not remote.calls


@pytest.mark.parametrize("outcome", ["declined", "unknown", "succeeded"])
async def test_host_logical_action_is_not_reissued_by_another_attempt(db, outcome):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    remote.fail = outcome == "unknown"
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)
        result = await svc.decide(
            OWNER,
            call["id"],
            approval(call, "decline" if outcome == "declined" else "accept"),
        )
        assert result["status"] == outcome
        led = Ledger(repo)
        await led.attempt_transition(attempt, AttemptState.FAILED)
        row = await repo.get_task(task)
        retry = (
            await led.open_attempt(
                task,
                step_id="direct",
                step_version="1",
                meta={
                    "kind": "formal_method",
                    "slot": "direct",
                    "snapshot_digest": row["pack_digest"],
                },
            )
        )["attempt_id"]
        await led.attempt_transition(retry, AttemptState.RUNNING)
        repeated = await svc.request_host_call(OWNER, task, retry)
        assert repeated["id"] == call["id"] and repeated["status"] == outcome
        assert len(remote.calls) == (0 if outcome == "declined" else 1)


async def test_foreign_owner_cannot_request_or_decide_host_work(db):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        svc = host_service(WorkbenchRepository(s), record, remote)
        with pytest.raises(NotFound):
            await svc.request_host_call(OTHER, task, attempt)
        call = await svc.request_host_call(OWNER, task, attempt)
        with pytest.raises(NotFound):
            await svc.decide(OTHER, call["id"], approval(call))
        assert not remote.calls


async def test_committed_dispatch_survives_worker_loss_without_a_second_call(
    db, monkeypatch
):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)

        async def lost(*args):
            raise RuntimeError("worker disappeared after committing dispatch")

        monkeypatch.setattr(svc, "_dispatch_call", lost)
        with pytest.raises(RuntimeError, match="worker disappeared"):
            await svc.decide(OWNER, call["id"], approval(call))
        replacement = host_service(repo, record, remote)
        assert (await replacement.request_host_call(OWNER, task, attempt))[
            "status"
        ] == "dispatching"
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_calls SET updated_at=now()-interval '3 minutes' WHERE id=:id"
                ),
                {"id": call["id"]},
            )
        assert (await replacement.request_host_call(OWNER, task, attempt))[
            "status"
        ] == "unknown"
        assert (await replacement.decide(OWNER, call["id"], approval(call)))[
            "status"
        ] == "unknown"
        assert not remote.calls


async def test_replay_returns_historical_call_when_present_source_authority_changed(db):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)
        succeeded = await svc.decide(OWNER, call["id"], approval(call))
        expired = host_service(
            repo, record.model_copy(update={"status": "revoked"}), remote
        )
        assert await expired.request_host_call(OWNER, task, attempt) == succeeded
        assert len(remote.calls) == 1


async def test_unreviewed_provider_cost_cannot_create_host_approval(db):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        with pytest.raises(CapabilityError, match="zero incremental"):
            await host_service(
                repo, record, remote, operations=(OPERATION,)
            ).request_host_call(OWNER, task, attempt)
        assert (await repo.get_task(task))["state"] == TaskState.RUNNING
        assert not (await repo.get_attempt(attempt))["refs"].get("capability_call_id")
        assert not remote.calls


@pytest.mark.parametrize("lost_state", ["awaiting_approval", "dispatching"])
async def test_driver_recovers_expiry_and_lost_dispatch_without_another_action(
    db, lost_state
):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        call = await host_service(repo, record, remote).request_host_call(
            OWNER, task, attempt
        )
        async with repo.transaction():
            if lost_state == "awaiting_approval":
                await s.execute(
                    text(
                        "UPDATE workbench_interactions SET expires_at=now()-interval '1 minute' WHERE task_id=:id"
                    ),
                    {"id": task},
                )
            else:
                raw = await repo.capability_call(OWNER, call["id"])
                await repo.update_call(OWNER, call["id"], "dispatching", raw["detail"])
                await s.execute(
                    text(
                        "UPDATE workbench_calls SET updated_at=now()-interval '3 minutes' WHERE id=:id"
                    ),
                    {"id": call["id"]},
                )
    driver = FormalExecution(
        SqlWorkbenchStores(db), lambda repo: host_service(repo, record, remote)
    )
    assert await driver.drive(task) == TaskState.FAILED
    async with db() as s:
        repo = WorkbenchRepository(s)
        result = (await repo.get_attempt(attempt))["refs"]["formal_result"]
        assert result["outcome"] == (
            "rejected" if lost_state == "awaiting_approval" else "unknown"
        )
        assert (await repo.get_task(task))["active_interaction_id"] is None
    assert not remote.calls


async def test_driver_refuses_call_evidence_from_another_work_item(db):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)
        await svc.decide(OWNER, call["id"], approval(call, "decline"))
        async with repo.transaction():
            raw = await repo.capability_call(OWNER, call["id"])
            raw["detail"]["formal_host"]["case_id"] = "another-case"
            await repo.update_call(OWNER, call["id"], "declined", raw["detail"])
    driver = FormalExecution(
        SqlWorkbenchStores(db), lambda repo: host_service(repo, record, remote)
    )
    with pytest.raises(CapabilityError, match="different work"):
        await driver.drive(task)
    async with db() as s:
        assert not (await WorkbenchRepository(s).get_attempt(attempt))["refs"].get(
            "formal_result"
        )
    assert not remote.calls


async def test_settlement_rechecks_late_receipt_under_the_task_lock(db):
    _, _, record, _, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)
        await svc.decide(OWNER, call["id"], approval(call))
        async with repo.transaction():
            observed = await repo.capability_call(OWNER, call["id"])
            await repo.update_call(OWNER, call["id"], "unknown", observed["detail"])
        observed = await repo.capability_call(OWNER, call["id"])
        async with repo.transaction():
            current = deepcopy(observed["detail"])
            current["result"] = {"structuredContent": {"receipt": "sent"}}
            await repo.update_call(OWNER, call["id"], "succeeded", current)
    driver = FormalExecution(
        SqlWorkbenchStores(db), lambda repo: host_service(repo, record, remote)
    )
    assert (
        await driver._finish(task, attempt, "unknown", observed_call=observed)
        == TaskState.RUNNING
    )
    assert await driver.drive(task) == TaskState.SUCCEEDED
    async with db() as s:
        result = (await WorkbenchRepository(s).get_attempt(attempt))["refs"][
            "formal_result"
        ]
        assert result["outputs"] == {"receipt": "sent"}
    assert len(remote.calls) == 1


async def test_model_watchdog_and_cancel_do_not_invent_host_turn_holds(db):
    _, _, record, _, task, attempt = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        await host_service(repo, record, Remote()).request_host_call(
            OWNER, task, attempt
        )
        before = await repo.get_task(task)
        assert (
            await Ledger(repo).settle_unattended(
                task,
                expected_version=before["state_version"],
                reason={"error": "model watchdog"},
                stopped=False,
            )
            is None
        )
        assert (await repo.get_task(task))["state"] == TaskState.WAITING
        cancelled = await Ledger(repo).request_cancel(task, owner_actor_id=OWNER)
        assert cancelled["state"] == TaskState.CANCELLED and not cancelled["interrupt"]
        current = await repo.get_task(task)
        assert not current["original_input"].get("client_context", {}).get("turn_open")
        assert (await repo.get_attempt(attempt))["turn_ids"] == []


async def test_model_runner_restart_preserves_durable_host_approval(db):
    _, _, record, sid, task, attempt = await setup(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = host_service(repo, record, remote)
        call = await svc.request_host_call(OWNER, task, attempt)
        session = await repo.get_session(sid)
        async with repo.transaction():
            await repo.capability_revoke_sandbox(str(session["sandbox_id"]))
            expired = await repo.expire_pending_tool_approvals(
                sandbox_id=str(session["sandbox_id"]), reason="model runner restarted"
            )
        assert not expired
        assert (await svc.decide(OWNER, call["id"], approval(call)))[
            "status"
        ] == "succeeded"
        assert len(remote.calls) == 1
