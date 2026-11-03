"""Whole-run deadlines and final accounting, with real storage and scripted turns."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from test_knowbot_test_runs_db import (
    ANSWER,
    PLAN,
    Clock,
    KnowbotTestConductor,
    KnowbotTestService,
    RunRequest,
    ScriptedAppServer,
    WorkbenchRepository,
    accept,
    close,
    drive,
    request,
    runner_for,
    setup_test_computer,
    status,
    workbench_stores,
)
from test_knowbot_test_runs_db import (
    db as db,
)

from app.application.workbench.ledger import Ledger


@pytest.mark.parametrize("ending", ["timeout", "cancelled"])
async def test_finished_and_interrupted_case_costs_include_late_settlement(db, ending):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    clock = Clock()
    run_id = f"knowlab-cost-{ending}"
    try:
        await setup_test_computer(db, fake)
        body = request(run_id).model_dump(mode="json")
        body["cases"].append({**body["cases"][0], "case_id": "c2"})
        await accept(db, RunRequest.model_validate(body))
        conductor = KnowbotTestConductor(workbench_stores(db), now=clock)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()  # record c1
        first = await status(db, run_id)
        assert first["cases"][0]["status"] == "passed"
        first_cost = Decimal(first["cost"]["spent"])
        assert first_cost > 0
        await conductor.tick()  # start c2
        async with db() as s:
            repo = WorkbenchRepository(s)
            cases = await repo.list_test_cases(run_id)
            task_id = str(cases[1]["task_id"])
            attempt = await Ledger(repo).open_attempt(
                task_id, step_id="plan", step_version="1"
            )
        async with db() as s, s.begin():
            await s.execute(
                text("""update workbench_tasks
                    set budget = jsonb_set(budget, '{used}', '"3.125"')
                    where id = :t"""),
                {"t": task_id},
            )
        running = await status(db, run_id)
        assert Decimal(running["progress"]["spent"]) == first_cost + Decimal("3.125")
        if ending == "timeout":
            clock.at += timedelta(seconds=3601)
            await conductor.tick()
        else:
            async with db() as s:
                await KnowbotTestService(WorkbenchRepository(s)).cancel(run_id)
        done = await status(db, run_id)
        assert done["status"] == ending
        assert [c["status"] for c in done["cases"]] == ["passed", "error"]
        assert Decimal(done["cost"]["spent"]) == first_cost + Decimal("3.125")
        # Provider usage arrives after cancellation. Duplicate turn delivery must
        # neither lose the charge nor count it twice, nor overwrite the result.
        for _ in range(2):
            async with db() as s:
                await Ledger(WorkbenchRepository(s)).record_late_cost(
                    task_id,
                    attempt_id=attempt["attempt_id"],
                    consumed=Decimal("0.25"),
                    tokens={},
                    turn_id="late-paid-turn",
                )
        await conductor.tick()
        again = await status(db, run_id)
        assert again["cases"] == done["cases"]
        assert Decimal(again["cost"]["spent"]) == first_cost + Decimal("3.375")
        clock.at += timedelta(days=8)
        await conductor.cleanup()
        assert (await status(db, run_id))["cost"] == again["cost"]
    finally:
        await close(runner)
        await fake.close()


async def test_recorded_success_is_not_overwritten_by_a_late_conductor_tick(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    clock = Clock()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-late-finish"))
        conductor = KnowbotTestConductor(workbench_stores(db), now=clock)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()  # result recorded before deadline
        clock.at += timedelta(seconds=3601)
        await conductor.tick()
        assert (await status(db, "knowlab-late-finish"))["status"] == "passed"
    finally:
        await close(runner)
        await fake.close()
