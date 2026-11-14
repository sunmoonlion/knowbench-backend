"""Real accepted plans, owner confirmation, persisted authority and action approval."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from method_access_support import ACCESS
from sqlalchemy import text
from test_capabilities_db import OWNER, URL, Remote, approval, body, service
from test_execution_plan_db import open_step
from test_method_plans_db import accept, planned_attempt, task
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_capabilities import check_runtime_method
from app.domain.workbench.capabilities import (
    CapabilityError,
    MethodPermissionRevoke,
    SelectionPut,
    digest,
    tool_key,
)
from app.domain.workbench.errors import NotFound, StaleStateVersion
from app.domain.workbench.states import AttemptState
from app.infrastructure.workbench.repository import WorkbenchRepository


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
    "change", ["revoked", "expired", "narrowed", "nondelegable", "foreign_parent"]
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
            elif change == "nondelegable":
                auth["grants"][0]["delegable"] = False
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
