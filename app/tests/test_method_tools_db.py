"""Dynamic MCP authorization: observed execution, selected tools, no scope escape."""

import asyncio
import copy
import uuid

import pytest
from sqlalchemy import text
from test_execution_plan_db import open_step, prepared
from test_method_plans_db import task
from test_workbench_ledger_db import OWNER, seed
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_mcp_routes import URL, bearer, client
from test_workbench_mcp_routes import served as served

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_tools import (
    MethodToolGuard,
    bind_turn,
    capture_attempt,
)
from app.domain.workbench.errors import RecordsRefused
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


async def context(db, *, planning=False):
    tid, sid = await (task(db) if planning else prepared(db))
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_session(sid)
        sb = str(row["sandbox_id"])
        thread = "thread-" + sid
        async with repo.transaction():
            await session.execute(
                text("UPDATE workbench_sessions SET thread_id=:t WHERE id=:s"),
                {"t": thread, "s": sid},
            )
            await session.execute(
                text("UPDATE workbench_sandboxes SET relay_user='u-abc' WHERE id=:s"),
                {"s": sb},
            )
        if planning:
            row = await repo.get_task(tid)
            opened = await Ledger(repo).open_method_plan(
                tid, expected_version=row["state_version"]
            )
        else:
            opened = await open_step(db, tid)
    aid = opened["attempt_id"]
    return tid, sid, sb, thread, aid


def metadata(thread, *, turn="turn-one", call="call-one"):
    return {
        "threadId": thread,
        "callId": call,
        "x-codex-turn-metadata": {"thread_id": thread, "turn_id": turn},
    }


async def observed(db, ctx, name, args, *, turn="turn-one", call="call-one", bind=True):
    tid, sid, _, thread, aid = ctx
    async with db() as session:
        repo = WorkbenchRepository(session)
        if bind:
            assert await capture_attempt(repo, thread) == (tid, aid)
            assert await bind_turn(repo, (tid, aid), thread, turn)
        async with repo.transaction():
            if tid is None:
                await repo.append_event(
                    session_id=sid,
                    kind="turn",
                    event_type="turn/accepted",
                    payload={"by": "user", "turn": {"id": turn}},
                )
            await repo.append_event(
                session_id=sid,
                kind="item",
                event_type="item/started",
                payload={
                    "threadId": thread,
                    "turnId": turn,
                    "item": {
                        "id": call,
                        "type": "mcpToolCall",
                        "server": "sunmoon_workbench",
                        "tool": name,
                        "arguments": args,
                    },
                },
            )
    return metadata(thread, turn=turn, call=call)


async def check(db, ctx, name, args, meta, **more):
    async with db() as session:
        await MethodToolGuard(
            WorkbenchRepository(session),
            owner=OWNER,
            sandbox=ctx[2],
            observe_seconds=0,
            **more,
        ).check(name, args, meta)


async def test_selected_tool_requires_the_actual_turn_and_exact_observed_call(db):
    ctx = await context(db)
    args = {"task": ctx[0], "item": "document-one"}
    meta = await observed(db, ctx, "read_input", args)
    await check(db, ctx, "read_input", args, meta)
    for altered in (
        None,
        {},
        {**meta, "callId": "invented"},
        {**meta, "threadId": "other"},
        metadata(ctx[3], turn="old-turn"),
    ):
        with pytest.raises(RecordsRefused):
            await check(db, ctx, "read_input", args, altered)
    with pytest.raises(RecordsRefused, match="differs"):
        await check(db, ctx, "read_input", {**args, "item": "other-document"}, meta)
    async with db() as session:
        attempt = await WorkbenchRepository(session).get_attempt(ctx[4])
        assert attempt["turn_ids"] == []  # Binding does not claim fees are settled.


@pytest.mark.parametrize(
    "name,args",
    [
        ("compute", {}),
        ("request_data", {}),
        ("describe_items", {}),
        ("lookup", {}),
        ("read_input", {"scope": "market"}),
        ("read_input", {"conversation": str(uuid.uuid4())}),
        ("read_input", {"task": str(uuid.uuid4())}),
    ],
)
async def test_observed_but_unselected_or_escaped_scope_is_refused(db, name, args):
    ctx = await context(db)
    args = {"task": ctx[0], **args}
    meta = await observed(db, ctx, name, args)
    with pytest.raises(RecordsRefused):
        await check(db, ctx, name, args, meta)


async def test_planning_turn_cannot_execute_a_method_tool(db):
    ctx = await context(db, planning=True)
    args = {"task": ctx[0]}
    meta = await observed(db, ctx, "read_input", args)
    with pytest.raises(RecordsRefused, match="not selected"):
        await check(db, ctx, "read_input", args, meta)


async def test_cancel_revokes_bound_turn_and_cannot_rebind_old_attempt(db):
    ctx = await context(db)
    args = {"task": ctx[0]}
    meta = await observed(db, ctx, "read_input", args)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await Ledger(repo).request_cancel(ctx[0], owner_actor_id=OWNER)
        assert not await bind_turn(repo, (ctx[0], ctx[4]), ctx[3], "new-turn")
    with pytest.raises(RecordsRefused):
        await check(db, ctx, "read_input", args, meta)
    # Dropping task after cancellation must not restore the old global-token path.
    meta = await observed(db, ctx, "lookup", {}, call="late", bind=False)
    with pytest.raises(RecordsRefused):
        await check(db, ctx, "lookup", {}, meta)
    with pytest.raises(RecordsRefused):
        await check(db, ctx, "lookup", {}, None)


async def test_turn_binding_cannot_be_replaced_or_crossed_between_tasks(db):
    ctx = await context(db)
    other = await context(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert await bind_turn(repo, (ctx[0], ctx[4]), ctx[3], "one")
        assert not await bind_turn(repo, (ctx[0], ctx[4]), ctx[3], "two")
        assert not await bind_turn(repo, (ctx[0], other[4]), ctx[3], "one")
        assert not await bind_turn(repo, (ctx[0], ctx[4]), other[3], "one")


async def test_mcp_http_rejects_missing_metadata_and_unselected_tool_before_dispatch(
    served, db, monkeypatch
):
    from app.interfaces.mcp import workbench_mcp

    ctx = await context(db)
    dispatched = []

    async def unexpected(*args, **kwargs):
        dispatched.append(args)
        raise AssertionError("unauthorized call reached a tool")

    monkeypatch.setattr(workbench_mcp, "call", unexpected)
    args = {"task": ctx[0], "name": "irrelevant"}
    meta = await observed(db, ctx, "find_subject", args)
    async with client(served) as http:
        for envelope in ({}, {"_meta": meta}):
            response = await http.post(
                URL,
                headers=bearer(),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "find_subject", "arguments": args, **envelope},
                },
            )
            assert response.status_code == 200
            assert response.json()["result"]["isError"] is True
    assert dispatched == []


async def test_no_task_and_other_chat_cannot_bypass_dynamic_guard(db):
    ctx = await context(db)
    for name in ("lookup", "describe_items", "list_library"):
        with pytest.raises(RecordsRefused, match="metadata"):
            await check(db, ctx, name, {"scope": "market"}, None)
    # A real independent user chat still works while the method is running.
    _, sb, sid = await seed(db)
    thread = "thread-" + sid
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text("UPDATE workbench_sessions SET thread_id=:t WHERE id=:s"),
                {"t": thread, "s": sid},
            )
    chat = (None, sid, sb, thread, None)
    args = {"conversation": sid}
    meta = await observed(db, chat, "list_library", args, bind=False)
    await check(db, chat, "list_library", args, meta)
    attack = {"task": ctx[0]}
    meta = await observed(db, chat, "read_input", attack, call="attack", bind=False)
    with pytest.raises(RecordsRefused, match="does not belong"):
        await check(db, chat, "read_input", attack, meta)


async def test_metadata_is_not_a_substitute_for_sandbox_ownership(db):
    ctx = await context(db)
    other = await context(db)
    args = {"task": ctx[0]}
    meta = await observed(db, ctx, "read_input", args)
    with pytest.raises(RecordsRefused, match="context not found"):
        await check(db, other, "read_input", args, meta)


async def test_http_can_arrive_before_runner_observation_without_fail_open(db):
    ctx = await context(db)
    args = {"task": ctx[0]}

    async def delayed():
        await asyncio.sleep(0.1)
        await observed(db, ctx, "read_input", args)

    pending = asyncio.create_task(delayed())
    try:
        async with db() as session:
            await MethodToolGuard(
                WorkbenchRepository(session),
                owner=OWNER,
                sandbox=ctx[2],
                observe_seconds=1,
            ).check("read_input", args, metadata(ctx[3]))
    finally:
        await pending


async def test_tampered_plan_binding_is_refused_even_with_real_tool_event(db):
    ctx = await context(db)
    args = {"task": ctx[0]}
    meta = await observed(db, ctx, "read_input", args)
    async with db() as session:
        repo = WorkbenchRepository(session)
        attempt = await repo.get_attempt(ctx[4])
        refs = copy.deepcopy(attempt["refs"])
        refs["tools"].append("compute")
        async with repo.transaction():
            await repo.update_attempt(ctx[4], refs=refs)
    from app.domain.workbench.errors import MethodPlanInvalid

    with pytest.raises(MethodPlanInvalid):
        await check(db, ctx, "read_input", args, meta)


async def test_selected_http_call_still_uses_the_existing_data_grant(
    served, db, monkeypatch
):
    from test_workbench_mcp_routes import FakeDataMcp

    from app.interfaces.mcp import workbench_mcp

    ctx = await context(db)
    kb = FakeDataMcp()
    monkeypatch.setattr(workbench_mcp, "knowbase_v2", lambda: kb)
    args = {"task": ctx[0], "material_id": "document"}
    meta = await observed(db, ctx, "read_input", args)
    message = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "read_input", "arguments": args, "_meta": meta},
    }
    async with client(served) as http:
        bare = await http.post(URL, headers=bearer(), json=message)
        assert bare.json()["result"]["isError"] is True
        assert "has no data" in bare.json()["result"]["content"][0]["text"]
    assert kb.seen == []
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text(
                    "INSERT INTO auth_user(id, issuer, subject, roles, scopes) VALUES (:id, 'test', 'author', '[]', '[]')"
                ),
                {"id": OWNER},
            )
            await repo.insert_task_resolution(
                {
                    "task_id": ctx[0],
                    "resolution_id": "resolution",
                    "revision": 1,
                    "consumer": "expert:synthetic@1",
                    "params": {},
                    "grant_ref": "dynamic-task-grant",
                    "status": "ready",
                }
            )
    async with client(served) as http:
        allowed = await http.post(URL, headers=bearer(), json=message)
    assert allowed.json()["result"]["isError"] is False
    claims, forwarded = kb.seen[-1]
    assert claims["tools"] == ["read_input"]
    assert forwarded["params"]["arguments"] == {
        "material_id": "document",
        "run": "dynamic-task-grant",
    }
    for forbidden in ("run", "session"):
        forged = {**args, forbidden: "some-other-grant"}
        meta = await observed(db, ctx, "read_input", forged, call=forbidden)
        message["params"].update(arguments=forged, _meta=meta)
        async with client(served) as http:
            refused = await http.post(URL, headers=bearer(), json=message)
        assert refused.json()["result"]["isError"] is True
        assert (
            "assigned by the workbench"
            in refused.json()["result"]["content"][0]["text"]
        )
    assert len(kb.seen) == 1


@pytest.mark.parametrize(
    "phase", ["planning", "method", "wrong-turn", "author-deny", "tampered"]
)
async def test_native_approval_uses_the_frozen_active_method_and_turn(db, phase):
    from app.application.workbench.runner import SandboxLink
    from app.bootstrap.workbench import build_runner
    from app.infrastructure.workbench.publisher import RedisPublisher

    ctx = await context(db, planning=phase == "planning")
    runner = build_runner(
        db, publisher=RedisPublisher(None, "method-test"), runner_id="method-policy"
    )
    replies = []

    class Connection:
        async def respond(self, request, result):
            replies.append(result)

    async with db() as session:
        repo = WorkbenchRepository(session)
        assert await bind_turn(repo, (ctx[0], ctx[4]), ctx[3], "native-turn")
        link = SandboxLink(runner, await repo.get_sandbox(ctx[2]))
        link.client = Connection()
        if phase == "author-deny":
            async with repo.transaction():
                await repo.insert_tool_grant(
                    session_id=ctx[1],
                    task_id=ctx[0],
                    category="file_change_outside",
                    command_prefix=None,
                    granted_by=OWNER,
                )
        if phase == "tampered":
            attempt = await repo.get_attempt(ctx[4])
            async with repo.transaction():
                await repo.update_attempt(
                    ctx[4], refs={**attempt["refs"], "tools": ["compute"]}
                )
    params = {
        "threadId": ctx[3],
        "turnId": "old-turn" if phase == "wrong-turn" else "native-turn",
    }
    if phase == "author-deny":
        params["grantRoot"] = "/outside"
    await link.on_server_request(
        "native-call", "item/fileChange/requestApproval", params
    )
    assert replies == [{"decision": "accept" if phase == "method" else "decline"}]
    if phase == "author-deny":
        await link.on_server_request(
            "repeat", "item/fileChange/requestApproval", params
        )
        assert replies == [{"decision": "decline"}, {"decision": "decline"}]


async def test_completed_turn_cannot_replay_an_observed_call(db):
    ctx = await context(db)
    args = {"task": ctx[0]}
    meta = await observed(db, ctx, "read_input", args)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.append_event(
                session_id=ctx[1],
                kind="turn",
                event_type="turn/completed",
                payload={
                    "threadId": ctx[3],
                    "turn": {"id": "turn-one", "status": "completed"},
                },
            )
    with pytest.raises(RecordsRefused, match="not been verified"):
        await check(db, ctx, "read_input", args, meta)


async def test_cancelled_unbound_turn_cannot_claim_to_be_a_user_chat(db):
    ctx = await context(db)
    meta = await observed(db, ctx, "lookup", {}, bind=False)
    async with db() as session:
        await Ledger(WorkbenchRepository(session)).request_cancel(
            ctx[0], owner_actor_id=OWNER
        )
    with pytest.raises(RecordsRefused, match="not an accepted user turn"):
        await check(db, ctx, "lookup", {}, meta)
