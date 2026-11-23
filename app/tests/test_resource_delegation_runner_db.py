"""Real app-server protocol: delegated evidence withdrawal is a runtime stop."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from test_data_planning_runner_db import PlanningServer
from test_execution_plan_db import open_step
from test_resource_delegation_runtime_db import confirm, setup
from test_workbench_ledger_db import db as db
from test_workbench_runner_db import TOKEN, _close, turn_completed

from app.application.workbench.execution_plan import execution_for_task
from app.application.workbench.task_runtime import StepAssessment, TaskRuntime
from app.bootstrap.workbench import build_runner
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)


class DelegationServer(PlanningServer):
    async def handle(self, ws, msg):
        if msg["method"] == "mcpServerStatus/list":
            await ws.send(
                json.dumps(
                    {
                        "id": msg["id"],
                        "result": {
                            "data": [
                                {
                                    "name": "sunmoon_workbench",
                                    "runtimeStatus": "connected",
                                    "tools": {"read_input": {"name": "read_input"}},
                                }
                            ],
                            "nextCursor": None,
                        },
                    }
                )
            )
            return
        await super().handle(ws, msg)


async def started(db, fake):
    ctx = await setup(db)
    await confirm(db, ctx)
    opened = await open_step(db, ctx.tid)
    runner = build_runner(
        db,
        publisher=RedisPublisher(None, "resource-delegation"),
        runner_id="delegation-probe",
    )
    runner.capabilities = ctx.service
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_session(ctx.sid)
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_sandboxes SET app_server_url=:url, token_ref=:token WHERE id=:id"
                ),
                {
                    "url": f"ws://127.0.0.1:{fake.port}",
                    "token": f"inline:{TOKEN}",
                    "id": row["sandbox_id"],
                },
            )
        link = await runner.link_for(str(row["sandbox_id"]), repo)
    await runner._thread_for_task(ctx.tid, row, link)
    async with db() as session:
        thread = (await WorkbenchRepository(session).get_session(ctx.sid))["thread_id"]
    return ctx, opened, runner, link, thread


async def settled(db, ctx, opened, result):
    async with db() as session:
        repo = WorkbenchRepository(session)
        view = await execution_for_task(repo, await repo.get_task(ctx.tid))
        assert view is not None
    step = view.pack.step(0)
    assert step is not None
    driver: Any = SimpleNamespace()
    return await TaskRuntime(SqlWorkbenchStores(db), driver)._settle_step(
        ctx.tid,
        opened["attempt_id"],
        view,
        step,
        result,
        StepAssessment(),
        0,
        None,
    )


async def close(runner, fake, running=None):
    if running is not None and not running.done():
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    if runner is not None:
        readers = [
            link.client._reader
            for link in runner.links.values()
            if link.client is not None
        ]
        await _close(runner)
        await asyncio.wait_for(
            asyncio.gather(
                *(r for r in readers if r is not None), return_exceptions=True
            ),
            5,
        )
    await fake.close()


async def test_record_withdrawal_interrupts_real_turn_and_preserves_pending_fees(db):
    fake = DelegationServer(quiet=True)
    await fake.start()
    runner = running = None
    try:
        ctx, opened, runner, link, thread = await started(db, fake)
        running = asyncio.create_task(
            link.run_turn(thread, "Use only the approved method scope", timeout=20)
        )
        for _ in range(300):
            if link.active_turns.get(thread):
                break
            if running.done():
                raise AssertionError(f"Provider turn never started: {running.result()}")
            await asyncio.sleep(0.01)
        assert link.active_turns[thread] == "planning-turn"
        # This changes the operator snapshot used by new service instances. It
        # does not fabricate the persisted revocation or a provider notification.
        ctx.records.clear()
        result = await asyncio.wait_for(running, 15)
        assert result.permission_failure == "revoked"
        assert not result.permission_stop_unconfirmed
        assert result.turn_id == "planning-turn"
        interrupts = [r for r in fake.requests if r["method"] == "turn/interrupt"]
        assert [r["params"] for r in interrupts] == [
            {"threadId": thread, "turnId": "planning-turn"}
        ]
        assert thread not in fake.in_progress
        assert await turn_completed(db, ctx.sid)
        assert await settled(db, ctx, opened, result) == TaskState.WAITING
        async with db() as session:
            repo = WorkbenchRepository(session)
            attempt = await repo.get_attempt(opened["attempt_id"])
            assert attempt["refs"]["permission_revocation"]
            assert attempt["refs"]["usage_pending"]["turn_id"] == "planning-turn"
            task = await repo.get_task(ctx.tid)
            wait = await repo.get_interaction(str(task["active_interaction_id"]))
            assert wait["prompt"]["subject"]["method_permission_revoked"]
            assert "费用待核对" in wait["prompt"]["question"]
        ctx.records.append(ctx.record)
        async with db() as session:
            view = await ctx.service(WorkbenchRepository(session)).selection(
                str(ctx.record.owner_actor_id), ctx.sid
            )
            assert view["method"]["permission_status"] == "revoked"
        assert (await confirm(db, ctx))["method"]["permission_status"] == "active"
        assert not ctx.remote.calls
        assert len([r for r in fake.requests if r["method"] == "turn/start"]) == 1
    finally:
        await close(runner, fake, running)


async def test_record_withdrawal_before_prepare_has_explicit_unstarted_zero_cost(db):
    fake = DelegationServer(quiet=True)
    await fake.start()
    runner = None
    try:
        ctx, opened, runner, link, thread = await started(db, fake)
        ctx.records.clear()
        result = await asyncio.wait_for(link.run_turn(thread, "work", timeout=10), 10)
        assert result.not_started and result.platform_failure
        assert result.turn_id is None and result.cost == 0
        assert not [r for r in fake.requests if r["method"] == "turn/start"]
        # Existing generic pre-dispatch configuration errors terminate the Task.
        # This test records that boundary, not an unsupported automatic recovery.
        assert await settled(db, ctx, opened, result) == TaskState.FAILED
        async with db() as session:
            repo = WorkbenchRepository(session)
            attempt = await repo.get_attempt(opened["attempt_id"])
            assert not attempt["refs"].get("usage_pending")
            task = await repo.get_task(ctx.tid)
            assert not task["original_input"].get("client_context", {}).get("turn_open")
            assert task["current_step"] == 0
        assert not ctx.remote.calls
    finally:
        await close(runner, fake)
