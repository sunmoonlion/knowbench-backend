"""Real task locks/transactions; synthetic inputs, no model or network calls."""

import asyncio
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from method_plan_fixtures import catalog, proposal
from sqlalchemy import text
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.ledger import Ledger
from app.application.workbench.method_plans import read_plan
from app.domain.workbench.errors import (
    BudgetExhausted,
    IdempotencyConflict,
    MethodPlanInvalid,
    StaleStateVersion,
)
from app.domain.workbench.method_plan import PLAN_ARTIFACT, MethodPlan, output_name
from app.domain.workbench.states import AttemptState, TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


async def task(db):
    _, _, sid = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        tid = (
            await led.handover(handover(sid, key=f"plan-{sid}"), owner_actor_id=OWNER)
        )["task_id"]
        # Intake still rejects v3 until unit 3. Seed the future pinned snapshot
        # only in this disposable test database; production has no such backdoor.
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_tasks SET pack_source='knowmart', pack_snapshot=cast(:p as jsonb), "
                    "pack_digest=:d, profile_version='1' WHERE id=:id"
                ),
                {
                    "p": json.dumps(catalog().model_dump(mode="json")),
                    "d": "sha256:" + "a" * 64,
                    "id": tid,
                },
            )
        await led.transition(tid, TaskState.VALIDATING)
        await led.transition(tid, TaskState.QUEUED)
        return tid, sid


async def planned_attempt(db, tid):
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        row = await repo.get_task(tid)
        aid = (await led.open_method_plan(tid, expected_version=row["state_version"]))[
            "attempt_id"
        ]
        await led.attempt_transition(aid, AttemptState.RUNNING)
        await led.attempt_transition(
            aid, AttemptState.COMPLETED, consumed=Decimal("0.02")
        )
        return aid, (await repo.get_task(tid))["state_version"]


async def accept(db, tid, aid, version, plan=None):
    async with db() as session:
        return await Ledger(WorkbenchRepository(session)).accept_method_plan(
            tid,
            attempt_id=aid,
            expected_version=version,
            proposal=proposal() if plan is None else plan,
        )


async def test_accept_restart_replay_and_same_attempt_different_content(db):
    tid, sid = await task(db)
    aid, version = await planned_attempt(db, tid)
    assert await accept(db, tid, aid, version) == {"version": 1, "replayed": False}
    assert await accept(db, tid, aid, version) == {"version": 1, "replayed": True}
    with pytest.raises(IdempotencyConflict):
        await accept(db, tid, aid, version, proposal(review=True))
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        stored = await read_plan(repo, row)
        assert stored and stored.proposal == proposal()
        assert row["pack_snapshot"] == catalog().model_dump(mode="json")
        assert row["budget"]["used"] == "0.02" and row["budget"]["reserved"] == "0.00"
        events = await repo.list_events(session_id=sid)
        assert sum(e["type"] == "task/plan_accepted" for e in events) == 1


async def test_plan_acceptance_retains_exact_independent_method_snapshot(db):
    from test_referenced_method_catalog import manifest

    snapshot = manifest()["pack"]
    tid, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_tasks SET pack_snapshot=cast(:p as jsonb) WHERE id=:id"
                ),
                {"p": json.dumps(snapshot), "id": tid},
            )
    aid, version = await planned_attempt(db, tid)
    assert await accept(db, tid, aid, version) == {"version": 1, "replayed": False}
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["pack_snapshot"] == snapshot
        assert (await read_plan(repo, row)).proposal == proposal()


async def test_same_catalog_two_tasks_keep_separate_plans_and_reject_cross_attempt(db):
    first, _ = await task(db)
    second, _ = await task(db)
    a, av = await planned_attempt(db, first)
    b, bv = await planned_attempt(db, second)
    with pytest.raises(MethodPlanInvalid):
        await accept(db, first, b, av)
    await accept(db, first, a, av)
    await accept(db, second, b, bv, proposal(review=True))
    async with db() as session:
        repo = WorkbenchRepository(session)
        left = await read_plan(repo, await repo.get_task(first))
        right = await read_plan(repo, await repo.get_task(second))
        assert left and right and left.proposal.steps != right.proposal.steps


async def test_concurrent_duplicate_acceptance_is_one_artifact_and_event(db):
    tid, sid = await task(db)
    aid, version = await planned_attempt(db, tid)
    results = await asyncio.gather(
        accept(db, tid, aid, version), accept(db, tid, aid, version)
    )
    assert sorted(r["replayed"] for r in results) == [False, True]
    async with db() as session:
        repo = WorkbenchRepository(session)
        rows = await repo.list_artifacts(tid)
        assert len(rows) == 1
        events = await repo.list_events(session_id=sid)
        assert sum(e["type"] == "task/plan_accepted" for e in events) == 1


@pytest.mark.parametrize("first", ["accept", "cancel"])
async def test_accept_and_cancel_serialize_on_task_lock(db, monkeypatch, first):
    tid, sid = await task(db)
    aid, version = await planned_attempt(db, tid)
    locked, release, contender = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def action(kind):
        async with db() as session:
            repo = WorkbenchRepository(session)
            original = repo.get_task

            async def get_task(task_id, *, for_update=False):
                if for_update and kind != first:
                    contender.set()
                row = await original(task_id, for_update=for_update)
                if for_update and kind == first:
                    locked.set()
                    await asyncio.wait_for(release.wait(), timeout=5)
                return row

            monkeypatch.setattr(repo, "get_task", get_task)
            led = Ledger(repo)
            if kind == "accept":
                return await led.accept_method_plan(
                    tid, attempt_id=aid, expected_version=version, proposal=proposal()
                )
            return await led.transition(tid, TaskState.CANCELLED)

    winner = asyncio.create_task(action(first))
    loser = None
    try:
        await asyncio.wait_for(locked.wait(), timeout=5)
        loser = asyncio.create_task(action("cancel" if first == "accept" else "accept"))
        await asyncio.wait_for(contender.wait(), timeout=5)
        release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(winner, loser, return_exceptions=True), timeout=5
        )
    finally:
        release.set()
        for running in (winner, loser):
            if running is not None and not running.done():
                running.cancel()
        await asyncio.gather(
            *(running for running in (winner, loser) if running is not None),
            return_exceptions=True,
        )
    assert isinstance(outcomes[0], dict)
    if first == "cancel":
        assert isinstance(outcomes[1], StaleStateVersion)
    else:
        assert isinstance(outcomes[1], dict)
        assert (await accept(db, tid, aid, version))["replayed"] is True
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["state"] == "CANCELLED"
        assert row["accepted_plan_version"] == (1 if first == "accept" else None)
        events = await repo.list_events(session_id=sid)
        assert sum(e["type"] == "task/plan_accepted" for e in events) == int(
            first == "accept"
        )


async def test_cancellation_before_late_plan_reply_never_revives_task(db):
    tid, _ = await task(db)
    aid, version = await planned_attempt(db, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await Ledger(repo).transition(tid, TaskState.CANCELLED)
        cancelled = await repo.get_task(tid)
    with pytest.raises(StaleStateVersion):
        await accept(db, tid, aid, version)
    with pytest.raises(MethodPlanInvalid):
        await accept(db, tid, aid, cancelled["state_version"])
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["state"] == "CANCELLED" and row["accepted_plan_version"] is None
        assert not await repo.list_artifacts(tid)


async def test_event_failure_rolls_back_artifact_and_pointer(db, monkeypatch):
    tid, _ = await task(db)
    aid, version = await planned_attempt(db, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        original = repo.append_event

        async def fail(**kwargs):
            if kwargs["event_type"] == "task/plan_accepted":
                raise RuntimeError("injected event failure")
            return await original(**kwargs)

        monkeypatch.setattr(repo, "append_event", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await Ledger(repo).accept_method_plan(
                tid, attempt_id=aid, expected_version=version, proposal=proposal()
            )
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert (await repo.get_task(tid))["accepted_plan_version"] is None
        assert not await repo.list_artifacts(tid)
    assert (await accept(db, tid, aid, version))["version"] == 1


async def test_invalid_plan_and_stale_reply_leave_no_artifact_or_pointer(db):
    tid, _ = await task(db)
    aid, version = await planned_attempt(db, tid)
    raw = proposal().model_dump(mode="json")
    raw["steps"][1]["tools"] = ["read_input"]
    with pytest.raises(MethodPlanInvalid, match="tools"):
        await accept(db, tid, aid, version, MethodPlan.model_validate(raw))
    with pytest.raises(StaleStateVersion):
        await accept(db, tid, aid, version - 1)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["state_version"] == version
        assert row["accepted_plan_version"] is None
        assert not await repo.list_artifacts(tid)
    # Validation failure is not a new model call or budget settlement.
    await accept(db, tid, aid, version)


async def test_running_attempt_prevents_new_planner_and_late_acceptance(db):
    tid, _ = await task(db)
    aid, _ = await planned_attempt(db, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        await led.open_attempt(tid, step_id="inspect_1", step_version="1")
        row = await repo.get_task(tid)
        with pytest.raises(MethodPlanInvalid, match="idle"):
            await led.open_method_plan(tid, expected_version=row["state_version"])
        with pytest.raises(MethodPlanInvalid, match="idle"):
            await led.accept_method_plan(
                tid,
                attempt_id=aid,
                expected_version=row["state_version"],
                proposal=proposal(),
            )
        assert len(await repo.list_attempts(tid)) == 2


@pytest.mark.parametrize(
    "field,value", [("version", 2), ("attempt_id", "wrong"), ("proposal", {})]
)
async def test_corrupt_receipt_fails_closed_on_restart_and_replay(db, field, value):
    tid, _ = await task(db)
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    async with db() as session:
        repo = WorkbenchRepository(session)
        # Fault injection in an isolated test schema, never repair corrupt live data.
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_artifacts SET content=content || cast(:patch as jsonb) WHERE task_id=:t AND name=:n"
                ),
                {"patch": json.dumps({field: value}), "t": tid, "n": PLAN_ARTIFACT},
            )
        with pytest.raises(MethodPlanInvalid):
            await read_plan(repo, await repo.get_task(tid))
    with pytest.raises(MethodPlanInvalid):
        await accept(db, tid, aid, version)


async def test_failed_planning_attempts_consume_round_limit_and_budget(db):
    tid, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        for _ in range(3):
            row = await repo.get_task(tid)
            aid = (
                await led.open_method_plan(tid, expected_version=row["state_version"])
            )["attempt_id"]
            await led.attempt_transition(
                aid, AttemptState.FAILED, consumed=Decimal("0.01")
            )
        row = await repo.get_task(tid)
        assert row["budget"]["used"] == "0.03"
        with pytest.raises(MethodPlanInvalid, match="limit"):
            await led.open_method_plan(tid, expected_version=row["state_version"])
        assert len(await repo.list_attempts(tid)) == 3


async def test_insufficient_budget_does_not_create_planning_attempt(db):
    tid, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        async with repo.transaction():
            await repo.cas_task(
                tid,
                expected_version=row["state_version"],
                budget={
                    "currency": "CNY",
                    "limit": "0.01",
                    "used": "0",
                    "reserved": "0",
                },
            )
        row = await repo.get_task(tid)
        with pytest.raises(BudgetExhausted):
            await Ledger(repo).open_method_plan(
                tid, expected_version=row["state_version"]
            )
        assert not await repo.list_attempts(tid)


async def test_replan_pins_accepted_results_and_preserves_history(db):
    tid, _ = await task(db)
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        row = await repo.get_task(tid)
        step = (await led.open_method_step(tid, expected_version=row["state_version"]))[
            "attempt_id"
        ]
        await led.attempt_transition(step, AttemptState.RUNNING)
        async with repo.transaction():
            art = await repo.put_artifact(
                task_id=tid,
                attempt_id=step,
                name=output_name("inspect_1"),
                kind="step",
                content={"rows": ["synthetic"]},
            )
            await led.attempt_transition(
                step,
                AttemptState.COMPLETED,
                output_artifacts=[{k: art[k] for k in ("id", "name", "version")}],
            )
            row = await repo.get_task(tid)
            await repo.cas_task(
                tid, expected_version=row["state_version"], current_step=1
            )
    next_a, next_v = await planned_attempt(db, tid)
    raw = proposal(parent=1, review=True).model_dump(mode="json")
    with pytest.raises(MethodPlanInvalid, match="pin"):
        await accept(db, tid, next_a, next_v, MethodPlan.model_validate(raw))
    for step in raw["steps"][1:]:
        step["inputs"][0]["version"] = 1
    await accept(db, tid, next_a, next_v, MethodPlan.model_validate(raw))
    # Replaying the first attempt must not rewind the accepted pointer.
    assert (await accept(db, tid, aid, version))["version"] == 1
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert (await repo.get_task(tid))["accepted_plan_version"] == 2
        old = await repo.get_artifact(task_id=tid, name=PLAN_ARTIFACT, version=1)
        assert old and old["content"]["proposal"] == proposal().model_dump(mode="json")


@pytest.mark.parametrize("fault", ["missing", "unregistered", "failed", "wrong_kind"])
async def test_replan_refuses_unaccepted_result_even_when_cursor_claims_completed(
    db, fault
):
    tid, _ = await task(db)
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        row = await repo.get_task(tid)
        step = (await led.open_method_step(tid, expected_version=row["state_version"]))[
            "attempt_id"
        ]
        await led.attempt_transition(step, AttemptState.RUNNING)
        async with repo.transaction():
            outputs = []
            if fault != "missing":
                art = await repo.put_artifact(
                    task_id=tid,
                    attempt_id=step,
                    name=output_name("inspect_1"),
                    kind="unverified" if fault == "wrong_kind" else "step",
                    content={"rows": ["synthetic"]},
                )
                if fault != "unregistered":
                    outputs = [{k: art[k] for k in ("id", "name", "version")}]
            await led.attempt_transition(
                step,
                AttemptState.FAILED if fault == "failed" else AttemptState.COMPLETED,
                output_artifacts=outputs,
            )
            # Inject inconsistent recovery state: the cursor alone isn't evidence.
            row = await repo.get_task(tid)
            await repo.cas_task(
                tid, expected_version=row["state_version"], current_step=1
            )
    next_a, next_v = await planned_attempt(db, tid)
    raw = proposal(parent=1).model_dump(mode="json")
    raw["steps"][1]["inputs"][0]["version"] = 1
    with pytest.raises(MethodPlanInvalid, match="no valid accepted result"):
        await accept(db, tid, next_a, next_v, MethodPlan.model_validate(raw))
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert (await repo.get_task(tid))["accepted_plan_version"] == 1


async def test_later_attempt_supersedes_unaccepted_reply(db):
    tid, _ = await task(db)
    first, _ = await planned_attempt(db, tid)
    second, version = await planned_attempt(db, tid)
    with pytest.raises(MethodPlanInvalid, match="superseded"):
        await accept(db, tid, first, version)
    await accept(db, tid, second, version)


async def test_migration_rolls_back_empty_pointer_and_refuses_losing_history(db):
    path = Path(__file__).parents[1] / "alembic/versions/20261102_0035_method_plans.py"
    spec = importlib.util.spec_from_file_location("plan_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def change(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()

    async with db() as session:
        async with session.begin():
            connection = await session.connection()
            await connection.run_sync(change, "downgrade")
            await connection.run_sync(change, "upgrade")
    tid, _ = await task(db)
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    async with db() as session:
        with pytest.raises(RuntimeError, match="cannot discard"):
            async with session.begin():
                await (await session.connection()).run_sync(change, "downgrade")
