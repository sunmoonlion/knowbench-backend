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
