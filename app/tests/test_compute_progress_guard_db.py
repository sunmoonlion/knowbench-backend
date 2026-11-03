"""Empty computation loops stop without accepting output or losing turn costs."""

import asyncio
import json
from decimal import Decimal

import pytest
from test_data_planning_runner_db import PlanningServer
from test_workbench_advisor_db import close, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.domain.workbench.compute_progress import ComputeProgressGuard
from app.infrastructure.workbench.repository import WorkbenchRepository


def failed(identity, arguments=None):
    return {
        "id": identity,
        "type": "mcpToolCall",
        "server": "sunmoon_workbench",
        "tool": "compute",
        "status": "failed",
        "arguments": arguments or {},
        "result": {"content": [{"type": "text", "text": "task not found"}]},
    }


def test_only_failed_unbound_computations_count_and_notifications_are_deduplicated():
    guard = ComputeProgressGuard()
    for _ in range(5):
        guard.observe(failed("one"))
    assert guard.missing == 1 and guard.failure is None
    guard.observe(failed("other") | {"server": "another_server"})
    guard.observe(failed("read") | {"tool": "query_facts"})
    assert guard.missing == 1
    # Real calculation/domain failures with valid arguments are not this loop.
    guard.observe(failed("bound", {"task": "task", "expression": "x/0"}))
    assert guard.missing == 0
    guard.observe(failed("a"))
    guard.observe(failed("b"))
    guard.observe(failed("success") | {"status": "completed", "result": {}})
    assert guard.missing == 0
    for i in range(3):
        guard.observe(failed(f"missing-{i}"))
    assert guard.failure == "missing_compute_task"


class ComputeServer(PlanningServer):
    def __init__(self, stubborn):
        super().__init__()
        self.stubborn = stubborn
        self.starts = 0

    async def handle(self, ws, msg):
        if msg["method"] != "turn/start":
            return await super().handle(ws, msg)
        self.starts += 1
        thread, turn = msg["params"]["threadId"], "empty-compute"
        self.in_progress[thread] = turn
        await ws.send(json.dumps({"id": msg["id"], "result": {"turn": {"id": turn}}}))
        await asyncio.sleep(0.05)
        for i in range(3):
            await ws.send(
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": thread,
                            "turnId": turn,
                            "item": failed(str(i)),
                        },
                    }
                )
            )


@pytest.mark.parametrize("stubborn", [False, True])
async def test_formal_step_stops_empty_computation_loop_and_requires_confirmed_stop(
    db, stubborn
):
    fake = ComputeServer(stubborn)
    await fake.start()
    runner = None
    try:
        runner, _, _, task_id = await seed(db, fake, budget="2")
        await runner.run_once()
        await runner.wait_drivers(timeout=15)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            assert task["state"] != "SUCCEEDED"
            assert len(await repo.list_attempts(task_id)) == 1
            assert await repo.list_artifacts(task_id) == []
            assert Decimal(task["budget"]["limit"]) == 2
            if stubborn:
                assert task["original_input"]["client_context"]["turn_open"]
                assert task["thread_id"] in fake.in_progress
            else:
                assert task["state"] == "FAILED"
                assert Decimal(task["budget"]["used"]) > 0
                events = await repo.list_task_events(task_id, types=("task/state",))
                assert "连续3次" in events[-1]["payload"]["reason"]["error"]
                assert events[-1]["payload"]["reason"]["platform"] is True
        await runner.run_once()
        await runner.wait_drivers(timeout=2)
        assert fake.starts == 1
    finally:
        if runner:
            await close(runner)
        await fake.close()
