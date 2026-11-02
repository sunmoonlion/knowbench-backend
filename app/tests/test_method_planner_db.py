"""Real plan/attempt transactions with the existing sandbox turn port scripted."""

import asyncio
from decimal import Decimal

import pytest
from method_plan_fixtures import catalog, proposal
from test_execution_plan_db import complete, open_step
from test_method_plans_db import task
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_planner import MethodPlanner
from app.application.workbench.plan_receipts import read_plan
from app.application.workbench.turn import TurnResult
from app.domain.workbench.errors import MethodPlanInvalid
from app.domain.workbench.method_plan import MethodPlan
from app.domain.workbench.states import AttemptState, TaskState
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

pytestmark = pytest.mark.asyncio


class Driver:
    def __init__(self, result=None, callback=None):
        self.result = result or TurnResult(
            "plan-turn", proposal().model_dump_json(), {}, cost=Decimal("0.02")
        )
        self.callback = callback
        self.calls = []

    async def run_turn(self, thread_id, text, *, timeout):  # noqa: ASYNC109
        self.calls.append((thread_id, text, timeout))
        if self.callback:
            await self.callback()
        return self.result


async def test_planner_uses_existing_turn_and_commits_receipt_with_cost(db):
    tid, _ = await task(db)
    driver = Driver()
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid) == TaskState.QUEUED
    )
    assert len(driver.calls) == 1
    prompt = driver.calls[0][1]
    assert catalog().planning.method_text in prompt
    assert '"protected_prefix": 0' in prompt
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert (await read_plan(repo, row)).proposal == proposal()
        assert row["budget"]["used"] == "0.02"
        assert Decimal(row["budget"]["reserved"]) == 0
        assert row["current_step"] == 0
        attempts = await repo.list_attempts(tid)
        assert len(attempts) == 1 and attempts[0]["status"] == AttemptState.COMPLETED
        assert attempts[0]["turn_ids"] == ["plan-turn"]


@pytest.mark.parametrize(
    "reply",
    ["not JSON", proposal().model_dump_json().replace('"read_input"', '"shell"')],
)
async def test_bad_reply_never_commits_a_plan_and_retry_is_bounded(db, reply):
    tid, _ = await task(db)
    driver = Driver(TurnResult("bad", reply, {}, cost=Decimal("0.01")))
    planner = MethodPlanner(SqlWorkbenchStores(db), driver)
    for _ in range(1 + catalog().planning.max_replans):
        assert await planner.run(tid) == TaskState.QUEUED
    assert await planner.run(tid) == TaskState.FAILED
    assert len(driver.calls) == 1 + catalog().planning.max_replans
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] is None
        assert not await repo.list_artifacts(tid)
        assert all(
            a["status"] == AttemptState.FAILED for a in await repo.list_attempts(tid)
        )
        assert Decimal(row["budget"]["used"]) == Decimal("0.01") * len(driver.calls)
        assert Decimal(row["budget"]["reserved"]) == 0
    assert '"last_rejection": null' in driver.calls[0][1]
    if len(driver.calls) > 1:
        assert '"last_rejection": null' not in driver.calls[1][1]


async def test_cancel_during_planning_accounts_late_cost_once_and_never_accepts(db):
    tid, _ = await task(db)

    async def cancel():
        async with db() as session:
            await Ledger(WorkbenchRepository(session)).request_cancel(
                tid, owner_actor_id=OWNER
            )

    driver = Driver(callback=cancel)
    planner = MethodPlanner(SqlWorkbenchStores(db), driver)
    assert await planner.run(tid) == TaskState.CANCELLED
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert not await read_plan(repo, row)
        assert row["budget"]["used"] == "0.02"
        attempt = (await repo.list_attempts(tid))[0]
    assert (
        await planner._settle(tid, str(attempt["id"]), driver.result)
        == TaskState.CANCELLED
    )
    async with db() as session:
        assert (await WorkbenchRepository(session).get_task(tid))["budget"][
            "used"
        ] == "0.02"


@pytest.mark.parametrize(
    "flags, state",
    [
        ({"environment_lost": True}, TaskState.WAITING),
        ({"timed_out": True}, TaskState.FAILED),
        ({"interrupted": True}, TaskState.FAILED),
        ({"error": "provider unavailable"}, TaskState.FAILED),
    ],
)
async def test_partial_plan_from_failed_turn_is_not_accepted(db, flags, state):
    tid, _ = await task(db)
    driver = Driver(
        TurnResult(
            "failed", proposal().model_dump_json(), {}, cost=Decimal("0.01"), **flags
        )
    )
    assert await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid) == state
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] is None
        assert Decimal(row["budget"]["reserved"]) == 0
        assert row["budget"]["used"] == "0.01"


async def test_over_budget_reply_is_charged_but_not_accepted(db):
    tid, _ = await task(db)
    driver = Driver(
        TurnResult("expensive", proposal().model_dump_json(), {}, cost=Decimal("200"))
    )
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid)
        == TaskState.WAITING
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert (
            row["accepted_plan_version"] is None and row["waiting_reason"] == "RESOURCE"
        )
        assert Decimal(row["budget"]["used"]) == 200


async def test_plan_event_failure_rolls_back_receipt_completion_and_charge(
    db, monkeypatch
):
    tid, _ = await task(db)
    original = WorkbenchRepository.append_event

    async def broken(self, **kwargs):
        if kwargs["event_type"] == "task/plan_accepted":
            raise RuntimeError("event store fault")
        return await original(self, **kwargs)

    monkeypatch.setattr(WorkbenchRepository, "append_event", broken)
    with pytest.raises(RuntimeError, match="event store fault"):
        await MethodPlanner(SqlWorkbenchStores(db), Driver()).run(tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] is None
        assert Decimal(row["budget"]["used"]) == 0
        assert not await repo.list_artifacts(tid)
        assert (await repo.list_attempts(tid))[0]["status"] == AttemptState.CREATED


async def test_replanning_prompt_and_receipt_preserve_prefix_and_pin_results(db):
    tid, _ = await task(db)
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), Driver()).run(tid)
        == TaskState.QUEUED
    )
    await complete(db, tid, await open_step(db, tid))
    updated = proposal(parent=1, review=True).model_dump(mode="json")
    for step in updated["steps"][1:]:
        step["inputs"][0]["version"] = 1
    plan = MethodPlan.model_validate(updated)
    driver = Driver(
        TurnResult("replan", plan.model_dump_json(), {}, cost=Decimal("0.02"))
    )
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid, parent_version=1)
        == TaskState.QUEUED
    )
    assert '"protected_prefix": 1' in driver.calls[0][1]
    assert '"accepted_results": {"inspect_1": 1}' in driver.calls[0][1]
    opened = await open_step(db, tid)
    assert opened["step"].step_id == "review_1"
    assert list(opened["inputs"].values())[0]["version"] == 1
    async with db() as session:
        repo = WorkbenchRepository(session)
        receipt = await read_plan(repo, await repo.get_task(tid))
        assert receipt.version == 2 and receipt.proposal == plan


async def test_failed_proposal_can_be_corrected_after_restart(db):
    tid, _ = await task(db)
    assert (
        await MethodPlanner(
            SqlWorkbenchStores(db),
            Driver(TurnResult("bad", "{}", {}, cost=Decimal("0.01"))),
        ).run(tid)
        == TaskState.QUEUED
    )
    driver = Driver()
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid) == TaskState.QUEUED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] == 1 and row["budget"]["used"] == "0.03"
        assert [a["status"] for a in await repo.list_attempts(tid)] == [
            AttemptState.FAILED,
            AttemptState.COMPLETED,
        ]


async def test_concurrent_planners_do_not_send_two_turns(db):
    tid, _ = await task(db)
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_release():
        entered.set()
        await release.wait()

    driver = Driver(callback=wait_for_release)
    planner = MethodPlanner(SqlWorkbenchStores(db), driver)
    first = asyncio.create_task(planner.run(tid))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await planner.run(tid) == TaskState.RUNNING
        assert len(driver.calls) == 1
    finally:
        release.set()
        assert await asyncio.wait_for(first, timeout=5) == TaskState.QUEUED


async def test_reservation_failure_does_not_call_model_or_create_attempt(db):
    tid, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            row = await repo.get_task(tid)
            await repo.cas_task(
                tid,
                expected_version=row["state_version"],
                budget={**row["budget"], "limit": "0.01"},
            )
    driver = Driver()
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid)
        == TaskState.WAITING
    )
    assert not driver.calls
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert not await repo.list_attempts(tid)
        assert (await repo.get_task(tid))["waiting_reason"] == "RESOURCE"


async def test_replan_cannot_rewrite_completed_method(db):
    tid, _ = await task(db)
    await MethodPlanner(SqlWorkbenchStores(db), Driver()).run(tid)
    await complete(db, tid, await open_step(db, tid))
    changed = proposal(parent=1).model_dump(mode="json")
    changed["steps"][0]["tools"] = []
    driver = Driver(
        TurnResult(
            "rewrite",
            MethodPlan.model_validate(changed).model_dump_json(),
            {},
            cost=Decimal("0.01"),
        )
    )
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid, parent_version=1)
        == TaskState.QUEUED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] == 1 and row["current_step"] == 1
        events = await repo.list_task_events(tid, types=("task/plan_rejected",))
        assert "executed prefix" in events[-1]["payload"]["reason"]


async def test_planning_settlement_rejects_another_tasks_attempt(db):
    first, _ = await task(db)
    second, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(second)
        opened = await Ledger(repo).open_method_plan(
            second, expected_version=row["state_version"]
        )
    with pytest.raises(MethodPlanInvalid, match="not this task"):
        await MethodPlanner(SqlWorkbenchStores(db), Driver())._settle(
            first, opened["attempt_id"], Driver().result
        )
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert (await repo.get_attempt(opened["attempt_id"]))[
            "status"
        ] == AttemptState.CREATED
        assert Decimal((await repo.get_task(first))["budget"]["used"]) == 0


async def test_repeated_initial_dispatch_does_not_silently_replan_or_charge(db):
    tid, _ = await task(db)
    driver = Driver()
    planner = MethodPlanner(SqlWorkbenchStores(db), driver)
    assert await planner.run(tid) == TaskState.QUEUED
    assert await planner.run(tid) == TaskState.QUEUED
    assert len(driver.calls) == 1
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] == 1 and row["budget"]["used"] == "0.02"
        assert len(await repo.list_attempts(tid)) == 1
