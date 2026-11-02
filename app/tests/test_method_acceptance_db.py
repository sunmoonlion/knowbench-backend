"""Dynamic covers acceptance must use the same pinned input as dispatch."""

import json

import pytest
from test_execution_plan_db import complete, open_step, prepared
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.acceptance import judge
from app.application.workbench.acceptance_numbers import judge_numbers
from app.application.workbench.ledger import Ledger
from app.domain.workbench.errors import MethodPlanInvalid
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


async def test_covers_ignores_unaccepted_latest_input_and_accepts_pinned_rows(db):
    tid, _ = await prepared(db)
    art = await complete(db, tid, await open_step(db, tid), value={"id": "accepted"})
    opened = await open_step(db, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=None,
                name=art["name"],
                kind="step",
                content={"rows": [{"id": "unaccepted"}]},
            )
        row = await repo.get_task(tid)
        rules = opened["step"].acceptance
        for key, ok in [("accepted", True), ("unaccepted", False)]:
            verdict = await judge_numbers(
                judge(json.dumps({"rows": [{"id": key}]}), rules),
                rules,
                repo=repo,
                task=row,
            )
            assert verdict.ok is ok


async def test_covers_cannot_validate_cancelled_attempt(db):
    tid, _ = await prepared(db)
    await complete(db, tid, await open_step(db, tid), value={"id": "accepted"})
    opened = await open_step(db, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await Ledger(repo).request_cancel(tid, owner_actor_id=OWNER)
        rules = opened["step"].acceptance
        with pytest.raises(MethodPlanInvalid, match="no active method"):
            await judge_numbers(
                judge('{"rows": [{"id":"accepted"}]}', rules),
                rules,
                repo=repo,
                task=await repo.get_task(tid),
            )
