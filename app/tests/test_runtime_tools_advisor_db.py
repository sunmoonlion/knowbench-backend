"""Missing MCP inventory settles through the real Runner/Advisor/Ledger path."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock

from test_workbench_advisor_db import ScriptedAppServer, close, drive, seed
from test_workbench_ledger_db import db as db

from app.application.workbench import runner as runtime
from app.domain.workbench.states import PLATFORM_FAILURE
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_unavailable_tools_fail_without_advancing_or_generating_empty_report(
    db, monkeypatch
):
    # The scripted server lacks tools/list. Declared data requirements exercise
    # the same preflight used by frozen user experts; task setup stays unchanged.
    monkeypatch.setattr(
        runtime,
        "declared_data_tools",
        AsyncMock(return_value=frozenset({"query_facts"})),
    )

    class MissingToolsServer(ScriptedAppServer):
        async def handle(self, ws, msg):
            if msg["method"] == "mcpServerStatus/list":
                await ws.send(json.dumps({"id": msg["id"], "result": {"data": []}}))
            else:
                await super().handle(ws, msg)

    fake = MissingToolsServer(['{"plan":["no data"],"assumptions":[]}'])
    await fake.start()
    runner = None
    try:
        runner, sid, _, task_id = await seed(db, fake)
        await drive(runner)
        assert fake.turn_inputs == []
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            assert task["state"] == "FAILED"
            assert task["current_step"] == 0
            assert Decimal(task["budget"]["used"]) == 0
            assert (await repo.get_session(sid))["wheel"] == "user"
            [attempt] = await repo.list_attempts(task_id)
            assert attempt["status"] == "FAILED"
            assert attempt["failure_code"] == PLATFORM_FAILURE
            assert not await repo.list_artifacts_with_content(task_id)
            events = await repo.list_task_events(
                task_id, types=("step/accepted", "task/state")
            )
            assert not any(e["type"] == "step/accepted" for e in events)
            assert events[-1]["payload"]["reason"]["platform"] is True
    finally:
        if runner:
            await close(runner)
        await fake.close()
