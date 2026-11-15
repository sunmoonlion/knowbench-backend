"""Real admitted model method, ordinary user turn and local WS/HTTP endpoints."""

import asyncio
from uuid import uuid4

from formal_model_support import ModelServer
from sqlalchemy import text
from test_capabilities_db import OWNER
from test_formal_method_use_routes_db import application
from test_formal_model_catalog_db import model_admitted
from test_workbench_ledger_db import seed

from app.application.workbench.formal_session_tools import formal_tool_key
from app.application.workbench.session_service import SessionService
from app.bootstrap.workbench import build_runner
from app.domain.workbench.capabilities import SelectionPut
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository


async def setup(db, *, kind="chat", selected=True):
    frozen, request, factory, remote = await model_admitted(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.purge_test_run(str(request.run_id))
            await repo.release_leases(runner_id="formal-probe")
    fake = ModelServer()
    await fake.start()
    fake.tool_name = formal_tool_key(frozen.reference)
    _, sandbox, sid = await seed(db, root="/home/u/research/model-use")
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_sandboxes SET app_server_url=:url,token_ref='inline:test' WHERE id=:id"
                ),
                {"url": f"ws://127.0.0.1:{fake.port}", "id": sandbox},
            )
            await session.execute(
                text("UPDATE workbench_sessions SET kind=:kind WHERE id=:id"),
                {"kind": kind, "id": sid},
            )
        if selected:
            caps = factory(repo)
            current = await caps.selection(OWNER, sid)
            await caps.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=current["revision"], formal_methods=(frozen.reference,)
                ),
            )
    runner = build_runner(
        db,
        publisher=RedisPublisher(None, "test"),
        runner_id="formal-session-model",
        poll_seconds=0.05,
    )
    runner.capabilities = factory
    fake.app = application(db, factory)
    remote.calls.clear()
    await runner.run_once()
    return frozen, factory, remote, sid, sandbox, fake, runner


async def start_turn(db, runner, sid, sandbox):
    async with db() as session:
        repo = WorkbenchRepository(session)
        request_id = "user-model-" + uuid4().hex
        await SessionService(repo).record_user_turn_requested(
            sid,
            owner_actor_id=OWNER,
            text="Use the selected method",
            request_id=request_id,
        )
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sandbox,
                kind="turn.start",
                payload={
                    "text": "Use the selected method",
                    "request_id": request_id,
                    "by": "user",
                },
            )
    await runner.run_once()


async def wait_call(db, sid):
    for _ in range(250):
        async with db() as session:
            calls = await WorkbenchRepository(session).capability_calls(OWNER, sid)
            if calls:
                return calls[0]
        await asyncio.sleep(0.02)
    return None


async def wait_completed(db, sid):
    for _ in range(250):
        async with db() as session:
            events = await WorkbenchRepository(session).list_events(session_id=sid)
            if any(e["type"] == "turn/completed" for e in events):
                return events
        await asyncio.sleep(0.02)
    raise AssertionError("Ordinary user turn did not complete")


async def shutdown(runner, fake):
    # The production close signals transport shutdown; let the real notification
    # reader finish its current DB transaction before the fixture drops its schema.
    await fake.close()
    readers = []
    for link in runner.links.values():
        if link.client:
            await link.client.close()
            reader = getattr(link.client, "_reader", None)
            if reader:
                readers.append(reader)
    if readers:
        await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), 5)
