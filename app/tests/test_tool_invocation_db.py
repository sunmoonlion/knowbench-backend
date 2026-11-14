"""Shared host evidence and invocation-based idempotency through real service paths."""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from test_capabilities_db import OWNER, approval, prepared, service
from test_capability_routes_db import application
from test_workbench_ledger_db import db as db
from tool_invocation_support import observe_capability_call, request_call

from app.application.workbench.method_tools import MethodToolGuard
from app.domain.workbench.errors import Forbidden, RecordsRefused
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_same_arguments_distinct_calls_and_exact_retries(db):
    sid, _, key, token, remote = await prepared(db)
    args = {"to": "recipient@example.test"}
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote)
        first = await request_call(svc, sid, token, key, args, call_id="first")
        call = await svc.repo.capability_call(OWNER, first["call_id"])
        assert call["detail"]["invocation"]["call_id"] == "first"
        await svc.decide(OWNER, call["id"], approval(call))
        retry = await request_call(svc, sid, token, key, args, call_id="first")
        assert retry["call_id"] == first["call_id"] and retry["status"] == "succeeded"
        assert len(remote.calls) == 1
        second = await request_call(svc, sid, token, key, args, call_id="second")
        assert second["call_id"] != first["call_id"]
        call = await svc.repo.capability_call(OWNER, second["call_id"])
        await svc.decide(OWNER, call["id"], approval(call))
        assert len(remote.calls) == 2
        assert len(await svc.repo.capability_calls(OWNER, sid)) == 2


async def test_call_identity_cannot_be_reassigned_different_arguments(db):
    sid, _, key, token, remote = await prepared(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote)
        await request_call(
            svc, sid, token, key, {"to": "a@example.test"}, call_id="same"
        )
        with pytest.raises(Forbidden, match="identity was reused"):
            await request_call(
                svc, sid, token, key, {"to": "b@example.test"}, call_id="same"
            )
        assert len(await svc.repo.capability_calls(OWNER, sid)) == 1
        assert not remote.calls


async def test_host_evidence_cannot_cross_server_arguments_turn_or_completion(db):
    sid, _, key, token, remote = await prepared(db)
    args = {"to": "a@example.test"}
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        meta = await observe_capability_call(svc, sid, key, args)
        for malformed in (
            None,
            {},
            {**meta, "threadId": "forged"},
            {**meta, "callId": "invented"},
        ):
            with pytest.raises(RecordsRefused):
                await svc.verify_invocation(
                    sid, token, key, args, malformed, observe_seconds=0
                )
        with pytest.raises(RecordsRefused, match="differs"):
            await svc.verify_invocation(
                sid, token, key, {"to": "b@example.test"}, meta, observe_seconds=0
            )
        session = await repo.get_session(sid)
        guard = MethodToolGuard(
            repo, owner=OWNER, sandbox=str(session["sandbox_id"]), observe_seconds=0
        )
        with pytest.raises(RecordsRefused, match="differs"):
            await guard.check(key, args, meta)
        old_turn = meta["x-codex-turn-metadata"]["turn_id"]
        other = {
            **meta,
            "callId": "another-call",
            "x-codex-turn-metadata": {
                "thread_id": meta["threadId"],
                "turn_id": "another-turn",
            },
        }
        async with repo.transaction():
            await repo.append_event(
                session_id=sid,
                kind="item",
                event_type="item/started",
                payload={
                    "threadId": meta["threadId"],
                    "turnId": "another-turn",
                    "item": {
                        "id": "another-call",
                        "type": "mcpToolCall",
                        "server": "sunmoon_capabilities",
                        "tool": key,
                        "arguments": args,
                    },
                },
            )
        with pytest.raises(Forbidden, match="another execution turn"):
            await svc.verify_invocation(sid, token, key, args, other, observe_seconds=0)
        async with repo.transaction():
            await repo.append_event(
                session_id=sid,
                kind="turn",
                event_type="turn/completed",
                payload={"turnId": old_turn},
            )
        with pytest.raises(RecordsRefused):
            await svc.verify_invocation(sid, token, key, args, meta, observe_seconds=0)
        assert not remote.calls and not await repo.capability_calls(OWNER, sid)


async def test_normal_account_has_no_platform_provenance_exemption(db):
    sid, _, _, _, _ = await prepared(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        session = await repo.get_session(sid)
        with pytest.raises(RecordsRefused, match="metadata"):
            await MethodToolGuard(
                repo, owner=OWNER, sandbox=str(session["sandbox_id"]), observe_seconds=0
            ).check("find_subject", {"query": "example"}, None)


async def test_real_managed_route_requires_metadata_for_actions_skills_and_results(db):
    sid, _, key, token, remote = await prepared(db)
    app = application(db, remote)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        for name, args in [
            (key, {"to": "a@example.test"}),
            ("skill_read", {"skill_id": "uninstalled", "path": "SKILL.md"}),
            ("skill_files", {"skill_id": "uninstalled"}),
            ("action_result", {"call_id": "invented"}),
        ]:
            response = await http.post(
                f"/api/mcp/capabilities/{sid}",
                headers={"Authorization": "Bearer " + token},
                json={
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
            )
            assert "verified execution metadata" in response.text
        async with db() as s:
            svc = service(WorkbenchRepository(s), remote)
            args = {"to": "a@example.test"}
            meta = await observe_capability_call(svc, sid, key, args)
        response = await http.post(
            f"/api/mcp/capabilities/{sid}",
            headers={"Authorization": "Bearer " + token},
            json={
                "id": 2,
                "method": "tools/call",
                "params": {"name": key, "arguments": args, "_meta": meta},
            },
        )
        assert (
            json.loads(response.json()["result"]["content"][0]["text"])["status"]
            == "awaiting_approval"
        )
        assert not remote.calls
        async with db() as s:
            assert len(await WorkbenchRepository(s).capability_calls(OWNER, sid)) == 1


async def test_managed_request_waits_for_runner_turn_acceptance(db):
    import asyncio

    from sqlalchemy import text

    sid, _, key, token, remote = await prepared(db)
    args = {"to": "a@example.test"}
    async with db() as s:
        repo = WorkbenchRepository(s)
        meta = await observe_capability_call(service(repo), sid, key, args)
        async with repo.transaction():
            await s.execute(
                text(
                    "delete from workbench_session_events where session_id=:s and event_type='turn/accepted'"
                ),
                {"s": sid},
            )
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote)
        pending = asyncio.create_task(
            svc.request_call(sid, token, key, args, meta=meta)
        )
        try:
            await asyncio.sleep(0.05)
            assert not pending.done() and not remote.calls
            async with db() as event_session:
                repo = WorkbenchRepository(event_session)
                async with repo.transaction():
                    await repo.append_event(
                        session_id=sid,
                        kind="turn",
                        event_type="turn/accepted",
                        payload={
                            "by": "user",
                            "turn": {"id": meta["x-codex-turn-metadata"]["turn_id"]},
                        },
                    )
            assert (await pending)["status"] == "awaiting_approval"
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_pending_approval_cannot_follow_a_replaced_thread(db):
    from app.domain.workbench.capabilities import CapabilityError

    sid, _, key, token, remote = await prepared(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s), remote)
        request = await request_call(svc, sid, token, key, {"to": "a@example.test"})
        call = await svc.repo.capability_call(OWNER, request["call_id"])
        async with svc.repo.transaction():
            await svc.repo.set_session_thread(sid, "replacement-thread")
        with pytest.raises(CapabilityError, match="Authorization changed"):
            await svc.decide(OWNER, call["id"], approval(call))
        assert not remote.calls
