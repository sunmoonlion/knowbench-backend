"""Stopping a method without final usage must not release unknown expenditure."""

import asyncio
from decimal import Decimal

import pytest
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.ledger import Ledger
from app.domain.workbench.errors import BudgetExhausted, IdempotencyConflict
from app.domain.workbench.states import AttemptState, TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository


async def pending(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        task = (await led.handover(handover(sid, budget="10"), owner_actor_id=OWNER))[
            "task_id"
        ]
        await led.transition(task, TaskState.VALIDATING)
        await led.transition(task, TaskState.QUEUED)
        attempt = (
            await led.open_attempt(
                task, step_id="one", step_version="1", reserve=Decimal("6")
            )
        )["attempt_id"]
        await led.attempt_transition(attempt, AttemptState.RUNNING)
        await led.attempt_transition(
            attempt,
            AttemptState.FAILED,
            failure_code="method_permission_revoked",
            retryable=True,
            turn_id="pending-turn",
            usage_pending=True,
        )
        return sid, task, attempt


async def test_unreported_usage_keeps_own_reservation_and_blocks_overspend(db):
    sid, task, attempt = await pending(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        snapshot = await repo.get_attempt(attempt)
        assert snapshot["refs"]["usage_pending"] == {
            "turn_id": "pending-turn",
            "reserved": "6",
        }
        assert snapshot["turn_ids"] == []
        assert (await repo.get_task(task))["budget"]["reserved"] == "6"
        with pytest.raises(BudgetExhausted):
            await led.open_attempt(
                task, step_id="two", step_version="1", reserve=Decimal("5")
            )
        events = await repo.list_events(session_id=sid)
        closed = [e for e in events if e["type"] == "attempt/state"][-1]
        assert closed["payload"]["cost_status"] == "pending"
        assert closed["payload"]["consumed"] is None


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("3")])
async def test_final_receipt_is_exact_and_idempotent_without_releasing_other_attempt(
    db, amount
):
    _, task, attempt = await pending(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        next_attempt = (
            await led.open_attempt(
                task, step_id="two", step_version="1", reserve=Decimal("4")
            )
        )["attempt_id"]
        with pytest.raises(IdempotencyConflict):
            await led.record_late_cost(
                task,
                attempt_id=attempt,
                consumed=amount,
                tokens={},
                turn_id="wrong-turn",
            )
        assert (await repo.get_task(task))["budget"]["reserved"] == "10"

    async def reconcile():
        async with db() as s:
            await Ledger(WorkbenchRepository(s)).record_late_cost(
                task,
                attempt_id=attempt,
                consumed=amount,
                tokens={"input": 0},
                turn_id="pending-turn",
            )

    await asyncio.gather(reconcile(), reconcile())
    async with db() as s:
        repo = WorkbenchRepository(s)
        budget = (await repo.get_task(task))["budget"]
        assert Decimal(budget["used"]) == amount
        assert Decimal(budget["reserved"]) == Decimal("4")
        assert str((await repo.get_task(task))["active_attempt_id"]) == next_attempt
        snapshot = await repo.get_attempt(attempt)
        assert snapshot["turn_ids"] == ["pending-turn"]
        assert "usage_pending" not in snapshot["refs"]


async def test_partial_current_attempt_consumption_cannot_take_another_pending_reserve(
    db,
):
    _, task, _ = await pending(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        second = (
            await led.open_attempt(
                task, step_id="two", step_version="1", reserve=Decimal("4")
            )
        )["attempt_id"]
        await led.attempt_transition(second, AttemptState.RUNNING)
        await led.attempt_transition(
            second, AttemptState.SUSPENDED, consumed=Decimal("2"), turn_id="second-1"
        )
        await led.attempt_transition(second, AttemptState.RUNNING)
        await led.attempt_transition(
            second, AttemptState.FAILED, consumed=Decimal("3"), turn_id="second-2"
        )
        budget = (await repo.get_task(task))["budget"]
        assert Decimal(budget["reserved"]) == Decimal("6")
        assert Decimal(budget["used"]) == Decimal("5")


async def test_partial_report_above_original_reserve_cannot_leave_spendable_budget(db):
    _, task, _ = await pending(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        second = (
            await led.open_attempt(
                task, step_id="two", step_version="1", reserve=Decimal("4")
            )
        )["attempt_id"]
        await led.attempt_transition(second, AttemptState.RUNNING)
        await led.attempt_transition(
            second,
            AttemptState.FAILED,
            turn_id="partial-turn",
            usage_pending=True,
            pending_cost=Decimal("7"),
        )
        budget = (await repo.get_task(task))["budget"]
        assert Decimal(budget["reserved"]) == Decimal("13")
        assert Decimal(budget["used"]) == 0
        with pytest.raises(BudgetExhausted):
            await led.open_attempt(
                task, step_id="three", step_version="1", reserve=Decimal("1")
            )


@pytest.mark.parametrize("cancel", [True, False])
async def test_revocation_cancel_and_unattended_close_preserve_unknown_usage(
    db, cancel
):
    _, task, _ = await pending(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        second = (
            await led.open_attempt(
                task,
                step_id="two",
                step_version="1",
                reserve=Decimal("4"),
                meta={
                    "permission_revocation": {"context": "step"},
                    "runtime_turn_id": "cancel-turn",
                    "usage_observed": {"estimated_cost": "5", "complete": False},
                },
            )
        )["attempt_id"]
        await led.attempt_transition(second, AttemptState.RUNNING)
        if cancel:
            await led.request_cancel(task, owner_actor_id=OWNER)
        else:
            current = await repo.get_task(task)
            await led.settle_unattended(
                task,
                expected_version=current["state_version"],
                reason={},
                stopped=False,
            )
        snapshot = await repo.get_attempt(second)
        assert snapshot["refs"]["usage_pending"]["turn_id"] == "cancel-turn"
        assert snapshot["refs"]["usage_pending"]["reserved"] == "5"
        assert snapshot["turn_ids"] == []
        budget = (await repo.get_task(task))["budget"]
        assert Decimal(budget["reserved"]) == Decimal("11")
        assert Decimal(budget["used"]) == 0


async def test_late_partial_usage_increases_hold_without_claiming_final_cost(db):
    _, task, attempt = await pending(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        for _ in range(2):
            await led.observe_pending_usage(
                task,
                attempt_id=attempt,
                turn_id="pending-turn",
                estimated_cost=Decimal("9"),
                tokens={"input": 100},
            )
        snapshot = await repo.get_attempt(attempt)
        assert snapshot["refs"]["usage_pending"]["reserved"] == "9"
        assert snapshot["refs"]["usage_observed"]["complete"] is False
        assert snapshot["turn_ids"] == []
        budget = (await repo.get_task(task))["budget"]
        assert Decimal(budget["reserved"]) == Decimal("9")
        assert Decimal(budget["used"]) == 0
        with pytest.raises(IdempotencyConflict):
            await led.observe_pending_usage(
                task,
                attempt_id=attempt,
                turn_id="wrong",
                estimated_cost=Decimal("9"),
                tokens={},
            )


async def test_only_explicit_not_started_evidence_releases_an_unbound_hold(db):
    _, task, _ = await pending(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        second = (
            await led.open_attempt(
                task,
                step_id="two",
                step_version="1",
                reserve=Decimal("4"),
                meta={"permission_revocation": {"context": "step"}},
            )
        )["attempt_id"]
        await led.request_cancel(task, owner_actor_id=OWNER)
        with pytest.raises(IdempotencyConflict):
            await led.record_late_cost(
                task, attempt_id=second, consumed=Decimal("0"), tokens={}, turn_id=None
            )
        await led.record_late_cost(
            task,
            attempt_id=second,
            consumed=Decimal("0"),
            tokens={},
            turn_id=None,
            not_started=True,
        )
        assert Decimal((await repo.get_task(task))["budget"]["reserved"]) == Decimal(
            "6"
        )
        assert (await repo.get_attempt(second))["refs"]["usage_reconciled"][
            "not_started"
        ] is True
