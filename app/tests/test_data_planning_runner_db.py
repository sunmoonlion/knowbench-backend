"""Real WebSocket + isolated DB: stage-scoped interruption, usage and stop acknowledgement."""

import asyncio
import json

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_runner_db import FakeAppServer, _close, _started, _task


class PlanningServer(FakeAppServer):
    def __init__(self, *, complete=False, quiet=False):
        super().__init__()
        self.complete, self.quiet = complete, quiet

    async def handle(self, ws, msg):
        method, p = msg["method"], msg.get("params") or {}
        if method == "turn/start":
            tid, turn = p["threadId"], "planning-turn"
            self.in_progress[tid] = turn
            await ws.send(
                json.dumps({"id": msg["id"], "result": {"turn": {"id": turn}}})
            )
            # Wait until run_turn installs its waiter, as real model latency would.
            await asyncio.sleep(0.05)
            await ws.send(
                json.dumps(
                    {
                        "method": "turn/started",
                        "params": {"threadId": tid, "turn": {"id": turn}},
                    }
                )
            )
            if not self.quiet:
                for i in range(3):
                    await ws.send(
                        json.dumps(
                            {
                                "method": "item/completed",
                                "params": {
                                    "threadId": tid,
                                    "turnId": turn,
                                    "item": {
                                        "id": str(i),
                                        "type": "mcpToolCall",
                                        "server": "data",
                                        "tool": "list_inputs",
                                        "arguments": {},
                                        "result": {"inputs": []},
                                    },
                                },
                            }
                        )
                    )
            if self.complete:
                await self.finish(ws, tid, turn)
        elif method == "turn/interrupt" and not self.stubborn:
            await ws.send(json.dumps({"id": msg["id"], "result": {}}))
            await self.finish(ws, p["threadId"], p["turnId"])
        else:
            await super().handle(ws, msg)

    async def finish(self, ws, tid, turn):
        self.in_progress.pop(tid, None)
        await ws.send(
            json.dumps(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": tid,
                        "turnId": turn,
                        "tokenUsage": self.meter.report(tid, 1000),
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": tid,
                        "turn": {
                            "id": turn,
                            "status": "completed" if self.complete else "interrupted",
                        },
                    },
                }
            )
        )


@pytest.mark.parametrize(
    "stage,quiet,stubborn,expected",
    [
        ("data-plan", False, False, "repeated_result"),
        ("data-plan", True, False, "time_limit"),
        ("data-plan", False, True, None),
        ("s1", False, False, None),
    ],
)
async def test_guard_only_limits_data_planning_and_confirms_stop(
    db, monkeypatch, stage, quiet, stubborn, expected
):
    fake = PlanningServer(complete=stage == "s1", quiet=quiet)
    fake.stubborn = stubborn
    await fake.start()
    runner = None
    try:
        runner, sb, sid, thread = await _started(db, fake)
        _, attempt = await _task(db, sid, "planning", attempt=True)
        async with db() as session:
            await session.execute(
                text("update workbench_attempts set step_id=:step where id=:id"),
                {"step": stage, "id": attempt},
            )
            await session.commit()
        # Exercise the stage cap without waiting three minutes in an offline test.
        monkeypatch.setattr(
            "app.application.workbench.runner.DATA_PLAN_SECONDS", 0.15 if quiet else 2
        )
        result = await runner.links[sb].run_turn(
            thread, "data requirements", timeout=10
        )
        resumes = [r for r in fake.requests if r["method"] == "thread/resume"]
        if stage == "data-plan":
            assert resumes[-1]["params"]["config"]["web_search"] == "disabled"
        else:
            assert not resumes
        assert result.planning_failure == expected
        assert result.planning_stop_unconfirmed is stubborn
        interrupts = [r for r in fake.requests if r["method"] == "turn/interrupt"]
        assert bool(interrupts) is (stage == "data-plan")
        if interrupts:
            assert interrupts[0]["params"] == {
                "threadId": thread,
                "turnId": "planning-turn",
            }
        if not stubborn:
            assert result.cost is not None and result.cost > 0
            assert thread not in fake.in_progress
        else:
            assert fake.in_progress[thread] == "planning-turn"
    finally:
        if runner:
            await _close(runner)
        await fake.close()


async def test_native_search_restores_for_analysis_and_does_not_change_other_threads(
    db,
):
    fake = PlanningServer(complete=True, quiet=True)
    await fake.start()
    runner = None
    try:
        runner, sb, sid, thread = await _started(db, fake)
        _, attempt = await _task(db, sid, "scope-tools", attempt=True)
        async with db() as session:
            await session.execute(
                text("update workbench_attempts set step_id='data-plan' where id=:id"),
                {"id": attempt},
            )
            await session.commit()
        link = runner.links[sb]
        assert not (await link.run_turn(thread, "propose data")).error
        assert link.search_scopes[thread] is True
        async with db() as session:
            await session.execute(
                text("update workbench_attempts set step_id='s1' where id=:id"),
                {"id": attempt},
            )
            await session.commit()
        assert not (await link.run_turn(thread, "perform analysis")).error
        assert link.search_scopes[thread] is False
        resumes = [r["params"] for r in fake.requests if r["method"] == "thread/resume"]
        assert len(resumes) == 2
        assert resumes[0]["config"]["web_search"] == "disabled"
        assert "web_search" not in resumes[1]["config"]
        assert all(r["threadId"] == thread for r in resumes)
        assert not any(
            r["method"]
            in {
                "config/value/write",
                "config/batchWrite",
                "thread/archive",
                "thread/delete",
            }
            for r in fake.requests
        )
    finally:
        if runner:
            await _close(runner)
        await fake.close()


@pytest.mark.parametrize("blocker", ["active", "unsubscribe", "read_failure"])
async def test_stage_configuration_failure_never_spends_a_model_turn(db, blocker):
    class BlockedServer(PlanningServer):
        async def handle(self, ws, msg):
            if blocker == "unsubscribe" and msg["method"] == "thread/unsubscribe":
                await ws.send(
                    json.dumps({"id": msg["id"], "result": {"status": "notSubscribed"}})
                )
            elif blocker == "read_failure" and msg["method"] == "thread/read":
                await ws.send(json.dumps({"id": msg["id"], "result": {}}))
            else:
                await super().handle(ws, msg)

    fake = BlockedServer(complete=True)
    await fake.start()
    runner = None
    try:
        runner, sb, sid, thread = await _started(db, fake)
        _, attempt = await _task(db, sid, "blocked-tools", attempt=True)
        async with db() as session:
            await session.execute(
                text("update workbench_attempts set step_id='data-plan' where id=:id"),
                {"id": attempt},
            )
            await session.commit()
        if blocker == "active":
            fake.in_progress[thread] = "existing-turn"
        result = await runner.links[sb].run_turn(thread, "do not spend")
        assert result.platform_failure and result.cost == 0 and result.turn_id is None
        assert result.not_started and result.usage_known
        assert not any(r["method"] == "turn/start" for r in fake.requests)
        if blocker == "active":
            assert not any(r["method"] == "thread/unsubscribe" for r in fake.requests)
            assert fake.in_progress[thread] == "existing-turn"
    finally:
        if runner:
            await _close(runner)
        await fake.close()
