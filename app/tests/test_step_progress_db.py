from decimal import Decimal

import pytest
from test_data_planning_runner_db import PlanningServer
from test_method_tools_db import context
from test_workbench_advisor_db import close, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_tools import bind_turn
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize(
    "stubborn,quiet,reason",
    [
        (False, True, "no_progress"),
        (False, False, "repeated_result"),
        (True, True, "no_progress"),
    ],
)
async def test_fixed_step_waits_without_automatic_rework(
    db, monkeypatch, stubborn, quiet, reason
):
    monkeypatch.setattr(
        "app.domain.workbench.step_progress.STEP_IDLE_SECONDS", 0.2 if quiet else 20
    )
    fake = PlanningServer(quiet=quiet)
    fake.stubborn = stubborn
    await fake.start()
    runner = None
    try:
        runner, sid, _, tid = await seed(db, fake, budget="2")
        await runner.run_once()
        await runner.wait_drivers(timeout=15)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(tid)
            attempts = await repo.list_attempts(tid)
            assert len(attempts) == 1 and not await repo.list_artifacts(tid)
            attempt = attempts[0]
            assert attempt["refs"]["runtime_turn_id"] == "planning-turn"
            assert attempt["started_at"] is not None
            if stubborn:
                assert task["original_input"]["client_context"]["turn_open"]
                assert not await repo.list_interactions(
                    session_id=sid, status="pending"
                )
            else:
                assert task["state"] == "WAITING" and task["waiting_reason"] == "INPUT"
                assert Decimal(task["budget"]["used"]) > 0
                assert attempt["turn_ids"] == ["planning-turn"]
                pending = await repo.list_interactions(session_id=sid, status="pending")
                assert len(pending) == 1
                assert [o["id"] for o in pending[0]["prompt"]["options"]] == [
                    "rework",
                    "stop",
                ]
                events = await repo.list_task_events(tid, types=("step/stopped",))
                assert events[-1]["payload"]["reason"] == reason
        await runner.run_once()
        await runner.wait_drivers(timeout=2)
        assert len([r for r in fake.requests if r["method"] == "turn/start"]) == 1
    finally:
        if runner:
            await close(runner)
        await fake.close()


async def test_binding_and_cancelled_late_cost_once(db):
    tid, _, _, thread, aid = await context(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        assert await bind_turn(repo, (tid, aid), thread, "paid-turn")
        assert await bind_turn(repo, (tid, aid), thread, "paid-turn")
        attempt = await repo.get_attempt(aid)
        assert attempt["status"] == "RUNNING" and attempt["started_at"] is not None
        assert attempt["turn_ids"] == []
        task = await repo.get_task(tid)
        led = Ledger(repo)
        await led.request_cancel(tid, owner_actor_id=str(task["owner_actor_id"]))
        before = Decimal((await repo.get_task(tid))["budget"]["used"])
        for _ in range(2):
            await led.record_late_cost(
                tid,
                attempt_id=aid,
                consumed=Decimal(".25"),
                tokens={},
                turn_id="paid-turn",
            )
        task = await repo.get_task(tid)
        assert Decimal(task["budget"]["used"]) - before == Decimal(".25")
        assert (await repo.get_attempt(aid))["turn_ids"] == ["paid-turn"]


@pytest.mark.parametrize("human_wait", [False, True])
async def test_hard_timeout_waits_but_human_approval_does_not_consume_idle_time(
    db, monkeypatch, human_wait
):
    import asyncio

    from test_workbench_runner_db import _close, _started, _task

    fake = PlanningServer(quiet=True)
    await fake.start()
    runner = None
    try:
        runner, sb, sid, thread = await _started(db, fake)
        await _task(db, sid, "step-time-limit", attempt=True)
        monkeypatch.setattr(
            "app.domain.workbench.step_progress.STEP_IDLE_SECONDS",
            0.3 if human_wait else 180,
        )
        link = runner.links[sb]
        running = asyncio.create_task(
            link.run_turn(thread, "method", timeout=2 if human_wait else 0.15)
        )
        if human_wait:
            for _ in range(100):
                if link.turn_waiters:
                    break
                await asyncio.sleep(0.005)
            link._wait_started(thread)
            await asyncio.sleep(0.5)
            assert not running.done()
            assert not [r for r in fake.requests if r["method"] == "turn/interrupt"]
            link._wait_ended(thread)
        result = await asyncio.wait_for(running, 5)
        assert result.idle_failure == ("no_progress" if human_wait else "time_limit")
        assert not result.idle_stop_unconfirmed and result.cost > 0
    finally:
        if runner:
            await _close(runner)
        await fake.close()
