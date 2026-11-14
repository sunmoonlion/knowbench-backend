"""Real accepted plans, owner confirmation, persisted authority and action approval."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from method_access_support import ACCESS
from sqlalchemy import text
from test_capabilities_db import OWNER, URL, Remote, approval, body, service
from test_execution_plan_db import open_step
from test_method_plans_db import accept, planned_attempt, task
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.application.workbench.advisor import Advisor, StepAssessment
from app.application.workbench.execution_plan import execution_for_task
from app.application.workbench.interactions import answer_interaction
from app.application.workbench.ledger import Ledger
from app.application.workbench.method_capabilities import check_runtime_method
from app.application.workbench.turn import TurnResult
from app.domain.workbench.capabilities import (
    CapabilityError,
    MethodPermissionRevoke,
    SelectionPut,
    digest,
    tool_key,
)
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.states import AttemptState, TaskState
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)


async def setup(db):
    tid, sid = await task(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        asset = await svc.create_connection(OWNER, body())
        row = await repo.get_task(tid)
        pack = row["pack_snapshot"]
        contract = next(
            unit["contract"]
            for unit in pack["method_units"]
            if unit["contract"]["step_id"] == "inspect"
        )
        contract["access_requirements"] = [ACCESS]
        contract["managed_capabilities"] = {
            "tools": [
                {"url": URL, "name": "send", "requirement_id": ACCESS["requirement_id"]}
            ],
            "skills": [],
        }
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_tasks SET pack_snapshot=cast(:p as jsonb),pack_digest=:d WHERE id=:id"
                ),
                {"p": json.dumps(pack), "d": "sha256:" + digest(pack), "id": tid},
            )
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    key = tool_key(asset["id"], "send")
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        view = await svc.selection(OWNER, sid)
        assert view["method"]["permission_status"] == "unconfirmed"
        view = await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=view["revision"],
                tools=[key],
                skills=[],
                method_context=view["method"]["context"],
                purpose="Deliver the notice",
            ),
        )
        assert view["method"]["permission_status"] == "active"
    opened = await open_step(db, tid)
    async with db() as s:
        repo = WorkbenchRepository(s)
        prepared = await service(repo, remote).prepare(OWNER, sid, scene="method")
        token = prepared["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
    return tid, sid, key, token, remote, opened


async def test_child_identity_and_authority_survive_service_restart(db):
    tid, sid, key, token, remote, opened = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        tool = await repo.capability_call(OWNER, staged["call_id"])
        root = await repo.call_by_key(OWNER, sid, "task:" + tid)
        attempt = await repo.get_attempt(opened["attempt_id"])
        assert tool["parent_call_id"] == attempt["refs"]["method_call_id"] != root["id"]
        child = await repo.method_authorization(OWNER, tool["parent_call_id"])
        assert child["parent_call_id"] == root["id"]
        assert child["grants"][0]["delegable"] is False
        assert child["grants"][0]["recipient_call_id"] == child["call_id"]
        recorded_child = await repo.call_by_key(
            OWNER,
            sid,
            (
                await s.scalar(
                    text("SELECT call_key FROM workbench_calls WHERE id=:id"),
                    {"id": child["call_id"]},
                )
            ),
        )
        assert recorded_child["status"] == "waiting"
        # Reading/authorizing a resource does not require passing it as a value.
        assert attempt["input_artifact_versions"] == []
        assert child["grants"][0]["requirement"]["resources"][0]["values"] == [
            "test@example.test"
        ]
        assert await repo.method_authorization(OTHER, child["call_id"]) is None
    async with db() as s:
        repo = WorkbenchRepository(s)
        result = await service(repo, remote).decide(OWNER, tool["id"], approval(tool))
        assert result["status"] == "succeeded"
        assert len(remote.calls) == 1


async def test_expired_child_needs_explicit_reconfirmation_not_parent_renewal(db):
    tid, sid, key, _, remote, opened = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        attempt = await repo.get_attempt(opened["attempt_id"])
        child_id = attempt["refs"]["method_call_id"]
        async with repo.transaction():
            child = await repo.method_authorization(OWNER, child_id, lock=True)
            before = child["revision"]
            child["revision"] += 1
            child["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            await repo.save_method_authorization(OWNER, sid, child, previous=before)
        view = await svc.selection(OWNER, sid)
        assert view["method"]["permission_status"] == "expired"
        with pytest.raises(CapabilityError):
            await svc.prepare(OWNER, sid, scene="method")
        # No operation was dispatched. Finish this failed attempt through the
        # existing ledger, then explicitly confirm before a new attempt opens.
        await Ledger(repo).attempt_transition(
            opened["attempt_id"],
            AttemptState.FAILED,
            failure_code="authorization_expired",
        )
        view = await svc.selection(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=view["revision"],
                method_context=view["method"]["context"],
                tools=[key],
                skills=[],
                skill_mode="explicit",
                purpose="Deliver the notice",
            ),
        )
        renewed = await repo.method_authorization(OWNER, child_id)
        assert renewed["revision"] == child["revision"] + 1
        assert datetime.fromisoformat(renewed["expires_at"]) > datetime.now(UTC)
    retried = await open_step(db, tid)
    async with db() as s:
        repo = WorkbenchRepository(s)
        attempt = await repo.get_attempt(retried["attempt_id"])
        assert attempt["refs"]["method_call_id"] == child_id
        assert retried["attempt_id"] != opened["attempt_id"]
        await service(repo, remote).prepare(OWNER, sid, scene="method")
        row = (
            (
                await s.execute(
                    text("SELECT status,attempt_id FROM workbench_calls WHERE id=:id"),
                    {"id": child_id},
                )
            )
            .mappings()
            .one()
        )
        assert (
            row["status"] == "created"
            and str(row["attempt_id"]) == retried["attempt_id"]
        )
        assert not remote.calls


@pytest.mark.parametrize(
    "change", ["revoked", "expired", "narrowed", "recipient_removed", "foreign_parent"]
)
async def test_pending_action_rechecks_live_parent_after_restart(db, change):
    tid, sid, key, token, remote, _ = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        call = await repo.capability_call(OWNER, staged["call_id"])
        root = await repo.call_by_key(OWNER, sid, "task:" + tid)
        async with repo.transaction():
            auth = await repo.method_authorization(OWNER, root["id"], lock=True)
            before = auth["revision"]
            auth["revision"] += 1
            if change == "revoked":
                auth["revoked"] = True
            elif change == "expired":
                auth["expires_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
            elif change == "narrowed":
                auth["grants"][0]["requirement"]["resources"][0]["values"] = [
                    "another@example.test"
                ]
            elif change == "recipient_removed":
                auth["grants"][0]["recipient_call_id"] = None
            else:
                selection = await repo.capability_selection(OWNER, sid, lock=True)
                selection["runtime"]["method"]["call_id"] = root["id"]
                await repo.capability_runtime(OWNER, sid, selection["runtime"])
            await repo.save_method_authorization(OWNER, sid, auth, previous=before)
    async with db() as s:
        repo = WorkbenchRepository(s)
        with pytest.raises(CapabilityError):
            await service(repo, remote).decide(OWNER, call["id"], approval(call))
        assert not remote.calls
        assert (await repo.capability_call(OWNER, call["id"]))[
            "status"
        ] == "awaiting_approval"


async def test_revoke_is_owned_and_transactional_and_keeps_completed_evidence(db):
    _, sid, key, token, remote, _ = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        call = await repo.capability_call(OWNER, staged["call_id"])
        await svc.decide(OWNER, call["id"], approval(call))
        view = await svc.selection(OWNER, sid)
        cmd = MethodPermissionRevoke(
            revision=view["revision"], method_context=view["method"]["context"]
        )
        with pytest.raises(NotFound):
            await svc.revoke_method_permissions(OTHER, sid, cmd)
        with pytest.raises(StaleStateVersion):
            await svc.revoke_method_permissions(
                OWNER, sid, cmd.model_copy(update={"revision": 0})
            )
        assert (await svc.selection(OWNER, sid))["method"][
            "permission_status"
        ] == "active"
        revoked = await svc.revoke_method_permissions(OWNER, sid, cmd)
        assert revoked["method"]["permission_status"] == "revoked"
        assert (await repo.capability_call(OWNER, call["id"]))["status"] == "succeeded"
        assert len(remote.calls) == 1
        selected = await repo.capability_selection(OWNER, sid)
        with pytest.raises(CapabilityError):
            await check_runtime_method(
                repo, OWNER, await repo.get_session(sid), selected["runtime"]
            )


@pytest.mark.parametrize("outcome", ["pending", "succeeded", "unknown"])
@pytest.mark.parametrize(
    "usage_known,price_known", [(False, False), (True, False), (True, True)]
)
async def test_revoke_stops_settles_and_waits_for_explicit_reconfirmation(
    db, outcome, usage_known, price_known
):
    pending = outcome == "pending"
    tid, sid, key, token, remote, opened = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        call = await repo.capability_call(OWNER, staged["call_id"])
        if not pending:
            remote.fail = outcome == "unknown"
            await svc.decide(OWNER, call["id"], approval(call))
        selection = await svc.selection(OWNER, sid)
        revoked = await svc.revoke_method_permissions(
            OWNER,
            sid,
            MethodPermissionRevoke(
                revision=selection["revision"],
                method_context=selection["method"]["context"],
            ),
        )
        assert not revoked["editable"]  # The old turn has not confirmed its stop.
        before = await repo.get_task(tid)
        used_before = Decimal(before["budget"]["used"])
        view = await execution_for_task(repo, before)
        attempt = await repo.get_attempt(opened["attempt_id"])
        child_id = attempt["refs"]["method_call_id"]
        assert attempt["refs"]["permission_revocation"]
    advisor = Advisor(SqlWorkbenchStores(db), SimpleNamespace())
    result = TurnResult(
        turn_id="stopped-permission-turn",
        final_text="Untrusted partial output",
        tokens={},
        cost=Decimal("0.01") if price_known else None,
        usage_known=usage_known,
        interrupted=True,
        permission_failure="revoked",
    )
    for _ in range(2):  # A replay settles cost once and retains the same wait.
        assert (
            await advisor._settle_step(
                tid,
                opened["attempt_id"],
                view,
                view.pack.step(0),
                result,
                StepAssessment(),
                0,
                None,
            )
            == TaskState.WAITING
        )
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        row = await repo.get_task(tid)
        assert row["current_step"] == 0
        assert Decimal(row["budget"]["used"]) == used_before
        attempt = await repo.get_attempt(opened["attempt_id"])
        assert attempt["failure_code"] == "method_permission_revoked"
        assert attempt["status"] == AttemptState.FAILED
        assert attempt["refs"].get("usage_pending")
        # Incremental usage never proves the final interrupted-turn cost.
        wait_record = await repo.get_interaction(str(row["active_interaction_id"]))
        assert "费用待核对" in wait_record["prompt"]["question"]
        assert "stopped-permission-turn" not in attempt["turn_ids"]
        old_interaction = await repo.get_interaction(call["interaction_id"])
        assert old_interaction["status"] == ("cancelled" if pending else "consumed")
        calls = await svc.calls(OWNER, sid)
        original = next(c for c in calls if c["id"] == call["id"])
        assert original["status"] == ("expired" if pending else outcome)
        selection = await svc.selection(OWNER, sid)
        assert selection["editable"]
        assert selection["method"]["permission_status"] == "revoked"
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selection["revision"],
                tools=[key],
                skills=[],
                method_context=selection["method"]["context"],
                purpose="Deliver the notice",
            ),
        )
        assert (await repo.get_task(tid))["state"] == TaskState.WAITING
        events = await repo.list_task_events(tid, types=("interaction/opened",))
        wait = next(
            e["payload"]
            for e in reversed(events)
            if e["payload"]["interaction_id"] == str(row["active_interaction_id"])
        )
        await answer_interaction(
            repo,
            wait["interaction_id"],
            token=wait["token"],
            response={"answers": {"continue": "continue"}},
            owner_actor_id=OWNER,
        )
    retried = await open_step(db, tid)
    async with db() as s:
        repo = WorkbenchRepository(s)
        attempt = await repo.get_attempt(retried["attempt_id"])
        assert retried["attempt_id"] != opened["attempt_id"]
        assert attempt["refs"]["method_call_id"] == child_id
        assert "permission_revocation" not in attempt["refs"]
        await service(repo, remote).prepare(OWNER, sid, scene="method")
        assert len(remote.calls) == (0 if pending else 1)


async def test_unconfirmed_permission_stop_cannot_offer_a_retry(db):
    tid, sid, _, _, remote, opened = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        selection = await svc.selection(OWNER, sid)
        await svc.revoke_method_permissions(
            OWNER,
            sid,
            MethodPermissionRevoke(
                revision=selection["revision"],
                method_context=selection["method"]["context"],
            ),
        )
        view = await execution_for_task(repo, await repo.get_task(tid))
    advisor = Advisor(SqlWorkbenchStores(db), SimpleNamespace())
    result = TurnResult(
        turn_id="unconfirmed-permission-turn",
        final_text=None,
        tokens={},
        permission_stop_unconfirmed=True,
        error="method_permission_revoked: stop not confirmed",
    )
    assert (
        await advisor._settle_step(
            tid,
            opened["attempt_id"],
            view,
            view.pack.step(0),
            result,
            StepAssessment(),
            0,
            None,
        )
        == TaskState.FAILED
    )
    async with db() as s:
        repo = WorkbenchRepository(s)
        row = await repo.get_task(tid)
        assert (
            row["original_input"]["client_context"]["turn_open"]["attempt_id"]
            == opened["attempt_id"]
        )
        assert row["active_interaction_id"] is None
        assert not remote.calls


async def test_runner_takeover_expires_call_even_before_approval_deadline(db):
    _, sid, key, token, remote, _ = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        session = await repo.get_session(sid)
        async with repo.transaction():
            await repo.expire_pending_tool_approvals(
                sandbox_id=str(session["sandbox_id"]), reason="runner restarted"
            )
        calls = await svc.calls(OWNER, sid)
        assert (
            next(c for c in calls if c["id"] == staged["call_id"])["status"]
            == "expired"
        )
        assert not remote.calls


@pytest.mark.parametrize("not_started", [False, True])
async def test_revocation_before_dispatch_needs_explicit_not_started_evidence(
    db, not_started
):
    tid, sid, _, _, remote, opened = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        selection = await svc.selection(OWNER, sid)
        await svc.revoke_method_permissions(
            OWNER,
            sid,
            MethodPermissionRevoke(
                revision=selection["revision"],
                method_context=selection["method"]["context"],
            ),
        )
        view = await execution_for_task(repo, await repo.get_task(tid))
    result = TurnResult(
        turn_id=None,
        final_text=None,
        tokens={},
        cost=Decimal("0"),
        error="configuration failed before dispatch"
        if not_started
        else "turn/start response lost",
        not_started=not_started,
        usage_known=not_started,
    )
    advisor = Advisor(SqlWorkbenchStores(db), SimpleNamespace())
    state = await advisor._settle_step(
        tid,
        opened["attempt_id"],
        view,
        view.pack.step(0),
        result,
        StepAssessment(),
        0,
        None,
    )
    assert state == (TaskState.WAITING if not_started else TaskState.FAILED)
    async with db() as s:
        repo = WorkbenchRepository(s)
        task = await repo.get_task(tid)
        assert bool(task["active_interaction_id"]) is not_started
        assert (
            bool(task["original_input"].get("client_context", {}).get("turn_open"))
            is not not_started
        )
        assert not remote.calls


@pytest.mark.parametrize("not_started", [False, True])
async def test_cancel_after_revocation_preserves_late_unknown_usage(db, not_started):
    tid, sid, _, _, remote, opened = await setup(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        selection = await svc.selection(OWNER, sid)
        await svc.revoke_method_permissions(
            OWNER,
            sid,
            MethodPermissionRevoke(
                revision=selection["revision"],
                method_context=selection["method"]["context"],
            ),
        )
        view = await execution_for_task(repo, await repo.get_task(tid))
        await Ledger(repo).request_cancel(tid, owner_actor_id=OWNER)
        assert (await repo.get_attempt(opened["attempt_id"]))["refs"]["usage_pending"]
        used = Decimal((await repo.get_task(tid))["budget"]["used"])
    result = TurnResult(
        turn_id=None if not_started else "cancelled-turn",
        final_text=None,
        tokens={},
        cost=Decimal("0") if not_started else Decimal("0.01"),
        not_started=not_started,
        usage_known=True,
        permission_stop_unconfirmed=not not_started,
        error="cancel after revoke",
    )
    advisor = Advisor(SqlWorkbenchStores(db), SimpleNamespace())
    for _ in range(2):
        assert (
            await advisor._settle_step(
                tid,
                opened["attempt_id"],
                view,
                view.pack.step(0),
                result,
                StepAssessment(),
                0,
                None,
            )
            == TaskState.CANCELLED
        )
    async with db() as s:
        repo = WorkbenchRepository(s)
        task = await repo.get_task(tid)
        attempt = await repo.get_attempt(opened["attempt_id"])
        assert Decimal(task["budget"]["used"]) == used
        assert bool(attempt["refs"].get("usage_pending")) is not not_started
        if not not_started:
            assert Decimal(task["budget"]["reserved"]) >= Decimal("0.01")
            assert task["original_input"]["client_context"]["turn_open"]
        else:
            assert not task["original_input"]["client_context"].get("turn_open")
        assert task["active_interaction_id"] is None
