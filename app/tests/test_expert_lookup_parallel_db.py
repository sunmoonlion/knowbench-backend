"""Two real runner turns on one sandbox connection: lookup completes while work waits."""

import asyncio
import json
import uuid

from test_workbench_advisor_db import ANSWER, PLAN, ScriptedAppServer, close, seed
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.find_expert import FindExpertService
from app.infrastructure.workbench.repository import WorkbenchRepository


class ParallelServer(ScriptedAppServer):
    def __init__(self):
        super().__init__()
        self.work_started = asyncio.Event()
        self.release_work = asyncio.Event()
        self.starts = []
        self.turns = []
        self.work_turns = 0

    async def handle(self, ws, msg):
        method, p = msg["method"], msg.get("params") or {}
        if method == "thread/start":
            self.starts.append(p)
        if method != "turn/start":
            return await super().handle(ws, msg)
        tid, turn = p["threadId"], str(uuid.uuid4())
        text = p["input"][0]["text"]
        lookup = "find_expert" in text
        self.turns.append((lookup, p))
        await ws.send(
            json.dumps(
                {"id": msg["id"], "result": {"turn": {"id": turn, "threadId": tid}}}
            )
        )
        if not lookup:
            self.work_turns += 1
            self.work_started.set()
            await self.release_work.wait()
        await asyncio.sleep(0.05)
        reply = (
            json.dumps(
                {
                    "outcome": "none",
                    "picks": [],
                    "recommend": {"action": "direct", "reason": "No expert fits"},
                }
            )
            if lookup
            else (PLAN if self.work_turns == 1 else ANSWER)
        )
        await ws.send(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": tid,
                        "turnId": turn,
                        "item": {"type": "agentMessage", "id": "reply", "text": reply},
                    },
                }
            )
        )
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
                        "turn": {"id": turn, "status": "completed"},
                    },
                }
            )
        )


async def test_lookup_has_its_own_thread_environment_budget_and_completion(db):
    fake = ParallelServer()
    await fake.start()
    runner = None
    try:
        runner, sid, _, work = await seed(db, fake)
        await runner.run_once()
        await asyncio.wait_for(fake.work_started.wait(), 10)
        async with db() as s:
            repo = WorkbenchRepository(s)
            pid = str((await repo.get_session(sid))["project_id"])
            find = await FindExpertService(repo).start(
                owner_actor_id=OWNER,
                project_id=pid,
                question="找到可用的专家",
                idempotency_key="parallel-real-runner",
            )
        await runner.run_once()
        async with asyncio.timeout(10):
            while True:
                async with db() as s:
                    repo = WorkbenchRepository(s)
                    task = await repo.get_task(find["task_id"])
                    if task["state"] == "SUCCEEDED":
                        break
                await asyncio.sleep(0.05)
        async with db() as s:
            repo = WorkbenchRepository(s)
            original = await repo.get_task(work)
            assert original["state"] == "RUNNING"
            assert original["budget"]["used"] == "0"
            assert task["budget"]["used"] != "0"
            assert original["thread_id"] != task["thread_id"]
            assert (await repo.get_session(sid))["wheel"] == "advisor"
            assert (await repo.get_session(find["session_id"]))["wheel"] == "user"
        [(_, params)] = [pair for pair in fake.turns if pair[0]]
        assert params["environments"] == []
        assert params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
        assert params["approvalPolicy"] == "never"
        assert "cwd" not in params
        assert fake.starts[-1]["environments"] == []
        assert fake.starts[-1]["sandbox"] == "read-only"
        assert "cwd" not in fake.starts[-1]
        fake.release_work.set()
        await runner.wait_drivers(timeout=15)
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert (await repo.get_task(work))["state"] == "SUCCEEDED"
            assert (await repo.get_task(find["task_id"]))["budget"] == task["budget"]
    finally:
        fake.release_work.set()
        if runner:
            await runner.wait_drivers(timeout=15)
            await close(runner)
        await fake.close()
