"""Synthetic runner events for tests; production provenance verification stays enabled."""

from types import SimpleNamespace

import httpx

from app.domain.workbench.capabilities import digest
from app.infrastructure.workbench.repository import WorkbenchRepository


async def running_platform_world(db, replies):
    """Explicit test execution with a fixed allowlist, not a paused review turn.

    Preserve the project's real grants, records and ownership from world(). The
    scripted runner has already stopped; no production checks are monkeypatched.
    """
    import json

    from sqlalchemy import text
    from test_workbench_records_db import world

    from app.application.workbench.catalog import pack_for_task
    from app.application.workbench.ledger import Ledger

    fake, runner, ids = await world(db, replies)
    async with db() as s:
        repo = WorkbenchRepository(s)
        row = await repo.get_task(ids["task"])
        pack = pack_for_task(row)
        index = int(row["current_step"])
        step = pack.step(index)
        step = step.model_copy(
            update={
                "tools": (
                    "list_project_conversations",
                    "read_project_conversation",
                    "list_inputs",
                    "compute",
                    "request_data",
                )
            }
        )
        steps = list(pack.workflow)
        steps[index] = step
        snapshot = pack.model_copy(update={"workflow": tuple(steps)}).model_dump(
            mode="json"
        )
        async with repo.transaction():
            await s.execute(
                text("""UPDATE workbench_tasks SET
                state='QUEUED', active_interaction_id=NULL, active_attempt_id=NULL,
                accepted_plan_version=NULL WHERE id=:id"""),
                {"id": ids["task"]},
            )
            row = await repo.get_task(ids["task"])
            await repo.cas_task(
                ids["task"],
                expected_version=row["state_version"],
                pack_source="knowlab-test",
                pack_snapshot=json.dumps(snapshot),
                pack_digest=digest(snapshot),
            )
        await Ledger(repo).open_attempt(
            ids["task"], step_id=step.step_id, step_version=step.step_version
        )
    return fake, runner, ids


async def observe_capability_call(svc, sid, name, arguments, *, call_id=None):
    repo = svc.repo
    selected = await repo.capability_runtime_by_session(sid)
    runtime = selected["runtime"]
    session = await repo.get_session(sid)
    thread = session.get("thread_id") or "thread-" + sid
    turn = runtime.get("turn_id") or "turn-" + runtime.get("scope_id", sid)
    call_id = call_id or "call-" + digest([name, arguments])
    async with repo.transaction():
        if not session.get("thread_id"):
            await repo.set_session_thread(sid, thread)
        if runtime.get("status") == "prepared":
            await repo.capability_runtime_event(
                sid, runtime["scope_id"], turn, started=True
            )
        binding = runtime.get("method")
        if binding:
            attempt = await repo.get_attempt(binding["attempt_id"])
            if not (attempt.get("refs") or {}).get("runtime_turn_id"):
                await repo.update_attempt(
                    binding["attempt_id"],
                    refs={**(attempt.get("refs") or {}), "runtime_turn_id": turn},
                )
        else:
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
                    "id": call_id,
                    "type": "mcpToolCall",
                    "server": "sunmoon_capabilities",
                    "tool": name,
                    "arguments": arguments,
                },
            },
        )
    return {
        "threadId": thread,
        "callId": call_id,
        "x-codex-turn-metadata": {"thread_id": thread, "turn_id": turn},
    }


async def request_call(svc, sid, token, name, arguments, *, call_id=None):
    meta = await observe_capability_call(svc, sid, name, arguments, call_id=call_id)
    return await svc.request_call(sid, token, name, arguments, meta=meta)


class ObservedCapabilityTransport(httpx.ASGITransport):
    """Functional route tests also supply the runner event that precedes MCP HTTP."""

    def __init__(self, app, db):
        super().__init__(app=app)
        self.db = db

    async def handle_async_request(self, request):
        import json

        if request.url.path.startswith("/api/mcp/capabilities/"):
            message = json.loads(await request.aread())
            if message.get("method") == "tools/call":
                params = message["params"]
                if "_meta" not in params:
                    async with self.db() as s:
                        params["_meta"] = await observe_capability_call(
                            SimpleNamespace(repo=WorkbenchRepository(s)),
                            request.url.path.rsplit("/", 1)[-1],
                            params["name"],
                            params.get("arguments", {}),
                        )
                    headers = dict(request.headers)
                    headers.pop("content-length", None)
                    request = httpx.Request(
                        request.method,
                        request.url,
                        headers=headers,
                        json=message,
                        extensions=request.extensions,
                    )
        return await super().handle_async_request(request)


class ObservedPlatformTransport(httpx.ASGITransport):
    """Supply synthetic host observations for platform HTTP functional tests."""

    def __init__(self, app, db, sessions):
        super().__init__(app=app)
        self.db, self.sessions = db, sessions

    async def handle_async_request(self, request):
        import json
        import uuid

        from sqlalchemy import text

        message = json.loads(await request.aread())
        if message.get("method") == "tools/call":
            params = message["params"]
            args = params.get("arguments", {})
            sid = self.sessions["current"]
            if params["name"] != "read_project_conversation" and args.get(
                "conversation"
            ):
                sid = args["conversation"]
            elif args.get("scope") == "market":
                sid = self.sessions["chat"]
            async with self.db() as s:
                repo = WorkbenchRepository(s)
                session = await repo.get_session(sid)
                thread = session.get("thread_id") or "thread-" + sid
                turn, call = (
                    "http-turn-" + uuid.uuid4().hex,
                    "http-call-" + uuid.uuid4().hex,
                )
                async with repo.transaction():
                    await repo.set_session_thread(sid, thread)
                    await s.execute(
                        text(
                            "update workbench_sandboxes set relay_user='u-abc' where id=:s"
                        ),
                        {"s": str(session["sandbox_id"])},
                    )
                    if not session.get("active_task_id"):
                        await repo.append_event(
                            session_id=sid,
                            kind="turn",
                            event_type="turn/accepted",
                            payload={"by": "user", "turn": {"id": turn}},
                        )
                    else:
                        task = await repo.get_task(str(session["active_task_id"]))
                        attempt = await repo.get_attempt(str(task["active_attempt_id"]))
                        # Multiple HTTP calls belong to this same synthetic host turn.
                        turn = (attempt.get("refs") or {}).get(
                            "runtime_turn_id"
                        ) or turn
                        from app.application.workbench.method_tools import bind_turn

                        assert await bind_turn(
                            repo, (str(task["id"]), str(attempt["id"])), thread, turn
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
                                "tool": params["name"],
                                "arguments": args,
                            },
                        },
                    )
            params["_meta"] = {
                "threadId": thread,
                "callId": call,
                "x-codex-turn-metadata": {"thread_id": thread, "turn_id": turn},
            }
            headers = dict(request.headers)
            headers.pop("content-length", None)
            request = httpx.Request(
                request.method,
                request.url,
                headers=headers,
                json=message,
                extensions=request.extensions,
            )
        return await super().handle_async_request(request)
