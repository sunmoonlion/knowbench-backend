"""Revocation and approval serialize against the same persisted conversation."""

import asyncio

import pytest
from formal_session_model_support import setup, shutdown, start_turn, wait_call
from httpx import ASGITransport, AsyncClient
from test_capabilities_db import OWNER, approval
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_ledger_db import seed

from app.application.workbench.formal_method_calls import FormalMethodCalls
from app.application.workbench.formal_session_tools import FormalSessionTools
from app.domain.workbench.capabilities import SelectionPut
from app.domain.workbench.errors import Forbidden
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_session_state_lock_allows_event_foreign_key_and_cursor_progress(db):
    _, _, sid = await seed(db)

    async def write_event():
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                return await repo.append_event(
                    session_id=sid,
                    kind="runtime",
                    event_type="item/started",
                    payload={},
                )

    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.get_session(sid, for_update=True)
            # A writer owns the cursor advisory lock before its event's FK read.
            # It must finish while this action owns the stable Session row.
            first = await asyncio.wait_for(write_event(), 3)
            second = await repo.append_event(
                session_id=sid,
                kind="approval",
                event_type="interaction/opened",
                payload={},
            )
            assert second["cursor"] == first["cursor"] + 1


async def test_deselect_waits_for_approval_then_blocks_a_dispatch_still_negotiating(
    db, monkeypatch
):
    _, factory, remote, sid, sandbox, fake, runner = await setup(db)
    entered, release, saving = asyncio.Event(), asyncio.Event(), asyncio.Event()
    saved = asyncio.Event()
    tasks = []
    try:
        await start_turn(db, runner, sid, sandbox)
        call = await wait_call(db, sid)
        assert call, fake.failures
        async with db() as session:
            selected = await factory(WorkbenchRepository(session)).selection(OWNER, sid)
        original = FormalSessionTools.resolve

        async def pause_resolve(self, owner, reference):
            entered.set()
            await release.wait()
            return await original(self, owner, reference)

        monkeypatch.setattr(FormalSessionTools, "resolve", pause_resolve)

        async def call_checked(url, token, name, arguments, *, before_send):
            await asyncio.wait_for(saved.wait(), 5)
            await before_send()
            return await remote.call(url, token, name, arguments)

        monkeypatch.setattr(remote, "call_checked", call_checked, raising=False)

        async def deselect():
            async with db() as session:
                caps = factory(WorkbenchRepository(session))
                saving.set()
                await caps.save_selection(
                    OWNER, sid, SelectionPut(revision=selected["revision"])
                )
                saved.set()

        async with AsyncClient(
            transport=ASGITransport(fake.app), base_url="http://testserver"
        ) as http:
            approve = asyncio.create_task(
                http.post(
                    f"/api/workbench/capability-calls/{call['id']}/decision",
                    json=approval(call).model_dump(),
                )
            )
            tasks.append(approve)
            await asyncio.wait_for(entered.wait(), 5)
            save = asyncio.create_task(deselect())
            tasks.append(save)
            await asyncio.wait_for(saving.wait(), 5)
            await asyncio.sleep(0.1)
            assert not save.done(), (
                "Selection committed while old approval was checking"
            )
            release.set()
            response, _ = await asyncio.wait_for(asyncio.gather(approve, save), 10)
            assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed"
        assert not remote.calls
        async with db() as session:
            selected = await factory(WorkbenchRepository(session)).selection(OWNER, sid)
            assert selected["formal_methods"] == []
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await shutdown(runner, fake)


async def test_result_rechecks_token_after_waiting_for_conversation(db):
    _, factory, _, sid, sandbox, fake, runner = await setup(db)
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await start_turn(db, runner, sid, sandbox)
        call = await wait_call(db, sid)
        assert call, fake.failures
        config = fake.thread_configs[call["detail"]["invocation"]["thread_id"]][
            "mcp_servers.sunmoon_capabilities"
        ]
        token = config["http_headers"]["Authorization"].removeprefix("Bearer ")

        async def read_result():
            async with db() as session:
                caps = factory(WorkbenchRepository(session))
                original = caps.runtime
                first = True

                async def pause_initial_runtime(*args):
                    nonlocal first
                    value = await original(*args)
                    if first:
                        first = False
                        entered.set()
                        await release.wait()
                    return value

                caps.runtime = pause_initial_runtime
                return await FormalMethodCalls(caps).model_result(
                    sid, token, call["id"]
                )

        task = asyncio.create_task(read_result())
        await asyncio.wait_for(entered.wait(), 5)
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                await repo.capability_revoke_sandbox(sandbox)
        release.set()
        with pytest.raises(Forbidden):
            await asyncio.wait_for(task, 5)
        # The human's retained receipt remains available after runtime revocation.
        async with db() as session:
            history = await FormalMethodCalls(
                factory(WorkbenchRepository(session))
            ).list_calls(OWNER, sid)
            assert len(history) == 1
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await shutdown(runner, fake)
