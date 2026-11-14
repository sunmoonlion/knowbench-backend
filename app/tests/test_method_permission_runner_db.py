"""Revocation interrupts the bound provider turn and requires stop acknowledgement."""

import asyncio
import json

import pytest
from sqlalchemy import text
from test_data_planning_runner_db import PlanningServer
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_runner_db import FakeAppServer, _close, _restart, _started, _task

from app.application.workbench.ledger import Ledger
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.states import WaitingReason
from app.infrastructure.workbench.repository import WorkbenchRepository


class MissingUsageServer(PlanningServer):
    early_usage = False

    async def handle(self, ws, msg):
        await super().handle(ws, msg)
        if self.early_usage and msg["method"] == "turn/start":
            tid = msg["params"]["threadId"]
            await ws.send(
                json.dumps(
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": tid,
                            "turnId": "planning-turn",
                            "tokenUsage": self.meter.report(tid, 1000),
                        },
                    }
                )
            )

    async def finish(self, ws, tid, turn):
        self.in_progress.pop(tid, None)
        await ws.send(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": tid,
                        "turn": {"id": turn, "status": "interrupted"},
                    },
                }
            )
        )


@pytest.mark.parametrize(
    "stubborn,usage_known,early_usage",
    [
        (False, True, False),
        (True, False, False),
        (False, False, False),
        (False, True, True),
    ],
)
async def test_permission_revocation_stops_only_current_attempt(
    db, stubborn, usage_known, early_usage
):
    fake = (PlanningServer if usage_known and not early_usage else MissingUsageServer)(
        quiet=True
    )
    if isinstance(fake, MissingUsageServer):
        fake.early_usage = early_usage
    fake.stubborn = stubborn
    await fake.start()
    runner = None
    running = None
    try:
        runner, sb, sid, thread = await _started(db, fake)
        _, attempt = await _task(db, sid, "permission", attempt=True)
        link = runner.links[sb]
        running = asyncio.create_task(link.run_turn(thread, "work", timeout=20))
        for _ in range(200):
            if link.active_turns.get(thread):
                break
            await asyncio.sleep(0.01)
        assert link.active_turns[thread] == "planning-turn"
        link.capability_scopes[thread] = "confirmed-method-scope"
        async with db() as session:
            await session.execute(
                text("""UPDATE workbench_attempts SET refs=refs || cast(:r AS jsonb)
                        WHERE id=:id"""),
                {
                    "id": attempt,
                    "r": json.dumps(
                        {
                            "permission_revocation": {
                                "context": "a" * 64,
                                "revision": 2,
                            }
                        }
                    ),
                },
            )
            await session.commit()
        result = await asyncio.wait_for(running, 15)
        assert result.permission_failure == (None if stubborn else "revoked")
        assert result.permission_stop_unconfirmed is stubborn
        interrupts = [r for r in fake.requests if r["method"] == "turn/interrupt"]
        assert len(interrupts) == 1
        assert interrupts[0]["params"] == {
            "threadId": thread,
            "turnId": "planning-turn",
        }
        assert (thread in fake.in_progress) is stubborn
        assert result.usage_known is usage_known
        assert not result.usage_final  # No protocol notification promises a final bill.
        if usage_known:
            assert result.cost is not None and result.cost > 0
        else:
            assert result.cost is None
    finally:
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if runner:
            await _close(runner)
        await fake.close()


@pytest.mark.parametrize("pending_approval", [False, True])
async def test_restart_recovers_revoked_step_without_dispatching_another_turn(
    db, pending_approval
):
    fake = FakeAppServer()
    await fake.start()
    first = second = None
    try:
        first, _, sid, thread = await _started(db, fake)
        task_id, attempt_id = await _task(db, sid, "revoked-restart", attempt=True)
        fake.in_progress[thread] = "revoked-leftover-turn"
        async with db() as session:
            await session.execute(
                text("""UPDATE workbench_attempts SET refs=refs || cast(:r AS jsonb)
                        WHERE id=:id"""),
                {
                    "id": attempt_id,
                    "r": json.dumps(
                        {
                            "permission_revocation": {
                                "context": "a" * 64,
                                "revision": 2,
                            }
                        }
                    ),
                },
            )
            await session.commit()
            if pending_approval:
                await Ledger(WorkbenchRepository(session)).open_interaction(
                    task_id,
                    kind="tool_approval",
                    waiting_reason=WaitingReason.APPROVAL,
                    attempt_id=attempt_id,
                    prompt=InteractionPrompt(
                        title="外部操作",
                        question="是否允许？",
                        subject={"capability_call_id": "pending-action"},
                    ),
                )
        second = await _restart(db, first)
        before = len(fake.requests)
        await second.run_once()
        await second.wait_drivers(timeout=5)
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(task_id)
            attempt = await repo.get_attempt(attempt_id)
            assert task["state"] == "WAITING" and task["waiting_reason"] == "INPUT"
            assert attempt["failure_code"] == "method_permission_revoked"
            wait = await repo.get_interaction(str(task["active_interaction_id"]))
            assert "费用待核对" in wait["prompt"]["question"]
            assert wait["prompt"]["subject"]["method_permission_revoked"]
        requests = fake.requests[before:]
        assert [
            r["params"]["turnId"] for r in requests if r["method"] == "turn/interrupt"
        ] == ["revoked-leftover-turn"]
        assert not [r for r in requests if r["method"] == "turn/start"]
        # The replacement wait has a terminal attempt and must not be reconciled
        # as an orphaned turn on the next polling round.
        assert not await second._reconcile()
    finally:
        if second:
            await _close(second)
        elif first:
            await _close(first)
        await fake.close()


class PaginatedServer(FakeAppServer):
    """Current protocol metadata remains authoritative with empty turn pages."""

    async def handle(self, ws, msg):
        if msg["method"] in ("thread/resume", "thread/read"):
            tid = msg["params"]["threadId"]
            await ws.send(
                json.dumps(
                    {
                        "id": msg["id"],
                        "result": {
                            "thread": {
                                "id": tid,
                                "turns": [],
                                "status": {
                                    "type": "active"
                                    if tid in self.in_progress
                                    else "idle"
                                },
                            }
                        },
                    }
                )
            )
            return
        await super().handle(ws, msg)


@pytest.mark.parametrize("stubborn", [False, True])
async def test_paginated_restart_interrupts_persisted_turn_and_verifies_idle(
    db, stubborn
):
    fake = PaginatedServer()
    fake.stubborn = stubborn
    await fake.start()
    first = second = None
    try:
        first, _, sid, thread = await _started(db, fake)
        task_id, attempt_id = await _task(db, sid, "paginated-restart", attempt=True)
        fake.in_progress[thread] = "persisted-turn"
        async with db() as session:
            repo = WorkbenchRepository(session)
            attempt = await repo.get_attempt(attempt_id)
            await repo.update_attempt(
                attempt_id,
                refs={
                    **attempt["refs"],
                    "runtime_turn_id": "persisted-turn",
                    "permission_revocation": {"context": "a" * 64, "revision": 2},
                },
            )
            await session.commit()
        second = await _restart(db, first)
        second.stop_confirm_seconds = 0.05
        before = len(fake.requests)
        await second.run_once()
        await second.wait_drivers(timeout=5)
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(task_id)
            assert task["state"] == ("FAILED" if stubborn else "WAITING")
            if stubborn:
                assert task["active_interaction_id"] is None
            else:
                attempt = await repo.get_attempt(attempt_id)
                assert attempt["refs"]["usage_pending"]["turn_id"] == "persisted-turn"
        requests = fake.requests[before:]
        assert [
            r["params"]["turnId"] for r in requests if r["method"] == "turn/interrupt"
        ] == ["persisted-turn"]
        assert any(
            r["method"] == "thread/read" and r["params"]["includeTurns"] is False
            for r in requests
        )
        assert not any(r["method"] == "turn/start" for r in requests)
    finally:
        if second:
            await _close(second)
        elif first:
            await _close(first)
        await fake.close()
