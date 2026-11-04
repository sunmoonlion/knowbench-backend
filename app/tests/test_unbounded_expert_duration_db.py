"""Real app-server transport: uncapped phases remain explicitly interruptible."""

import asyncio

import pytest
from sqlalchemy import text
from test_data_planning_runner_db import PlanningServer
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_runner_db import _close, _started, _task

from app.domain.workbench.scheduling import LOOKUP_PROFILE


@pytest.mark.parametrize("stage", ["data-plan", "lookup", "s1"])
async def test_default_unbounded_turn_can_be_stopped_and_accounts_usage(db, stage):
    fake = PlanningServer(quiet=True)
    await fake.start()
    runner = None
    try:
        runner, sandbox, session, thread = await _started(db, fake)
        task, attempt = await _task(db, session, "unbounded", attempt=True)
        async with db() as connection:
            await connection.execute(
                text("update workbench_attempts set step_id=:step where id=:id"),
                {"step": stage, "id": attempt},
            )
            if stage == "lookup":
                await connection.execute(
                    text("update workbench_tasks set profile_id=:profile where id=:id"),
                    {"profile": LOOKUP_PROFILE, "id": task},
                )
            await connection.commit()
        link = runner.links[sandbox]
        running = asyncio.create_task(link.run_turn(thread, "work"))
        for _ in range(200):
            if link.turn_waiters:
                break
            await asyncio.sleep(0.01)
        assert link.turn_waiters
        await asyncio.sleep(0.3)
        assert not running.done()
        assert not any(r["method"] == "turn/interrupt" for r in fake.requests)
        await link.interrupt(thread)
        result = await asyncio.wait_for(running, 5)
        assert result.interrupted and not result.timed_out
        assert (
            not result.idle_failure
            and not result.planning_failure
            and not result.lookup_failure
        )
        assert result.cost is not None and result.cost > 0
        assert thread not in fake.in_progress
    finally:
        if runner:
            await _close(runner)
        await fake.close()
