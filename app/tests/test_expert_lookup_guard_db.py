"""Lookup must stop on directory failure, deadline or observed budget usage."""

import asyncio
import json
from decimal import Decimal

import pytest
from test_data_planning_runner_db import PlanningServer
from test_workbench_advisor_db import close, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.find_expert import FindExpertService
from app.domain.workbench.expert_lookup import LookupGuard
from app.infrastructure.workbench.repository import WorkbenchRepository


class LookupServer(PlanningServer):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.starts = 0
        self.stubborn = mode == "unconfirmed"

    async def handle(self, ws, msg):
        if msg["method"] != "turn/start":
            return await super().handle(ws, msg)
        self.starts += 1
        tid, turn = msg["params"]["threadId"], "lookup-turn"
        self.in_progress[tid] = turn
        await ws.send(json.dumps({"id": msg["id"], "result": {"turn": {"id": turn}}}))
        await asyncio.sleep(0.05)
        if self.mode == "quiet":
            return
        if self.mode == "budget":
            await ws.send(
                json.dumps(
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": tid,
                            "turnId": turn,
                            "tokenUsage": self.meter.report(tid, 1000000),
                        },
                    }
                )
            )
            return
        item = {
            "id": "find-1",
            "type": "mcpToolCall",
            "tool": "find_expert",
            "status": "failed",
            "result": {
                "isError": True,
                "content": [
                    {"type": "text", "text": "the records are temporarily unavailable"}
                ],
            },
        }
        if self.mode == "approval":
            item["result"] = None
            item["error"] = {
                "message": "MCP tool call requires approval, but approval policy is never"
            }
        await ws.send(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {"threadId": tid, "turnId": turn, "item": item},
                }
            )
        )


@pytest.mark.parametrize(
    "mode,code",
    [
        ("failure", "directory_unavailable"),
        ("approval", "tool_approval_required"),
        ("quiet", "time_limit"),
        ("budget", "budget_limit"),
        ("unconfirmed", None),
    ],
)
async def test_failed_lookup_settles_cost_without_retry_or_extra_budget(
    db, monkeypatch, mode, code
):
    fake = LookupServer(mode)
    await fake.start()
    runner = None
    try:
        runner, sid, _, task_id = await seed(
            db, fake, profile="FIND_EXPERT", budget="2"
        )
        monkeypatch.setattr(
            "app.application.workbench.runner.LOOKUP_SECONDS",
            0.2 if mode == "quiet" else 2,
        )
        await runner.run_once()
        await runner.wait_drivers(timeout=15)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            assert task["state"] == "FAILED"
            assert task["budget"]["limit"] == "2"
            assert len(await repo.list_attempts(task_id)) == 1
            assert await repo.get_artifact(task_id=task_id, name="find_expert") is None
            if code:
                events = await repo.list_task_events(task_id, types=("task/state",))
                assert events[-1]["payload"]["reason"]["error"] == code
                assert Decimal(str(task["budget"]["used"])) > 0
                assert not task["original_input"]["client_context"].get("turn_open")
                result = await FindExpertService(repo).result(
                    str(task["owner_actor_id"]), task_id
                )
                assert "已停止" in result["error"]
                assert "outcome" not in result
            else:
                assert task["original_input"]["client_context"]["turn_open"]
                assert task["thread_id"] in fake.in_progress
                assert (
                    await repo.active_task_of_project(
                        str(task["project_id"]), lane="lookup"
                    )
                ) is not None
        await runner.run_once()
        await runner.wait_drivers(timeout=2)
        assert fake.starts == 1
    finally:
        if runner:
            await close(runner)
        await fake.close()


def test_lookup_guard_deduplicates_calls_and_preserves_success_or_unknown_usage():
    guard = LookupGuard(Decimal("2"))
    success = {
        "id": "a",
        "type": "mcpToolCall",
        "tool": "find_expert",
        "status": "completed",
        "result": {"experts": []},
    }
    for _ in range(5):
        guard.observe(success)
    assert guard.calls == 1
    guard.usage(None)
    guard.usage("0.01")
    assert guard.failure is None
    guard.observe(success | {"id": "b"})
    guard.observe(success | {"id": "c"})
    assert guard.failure == "repeated_lookup"
