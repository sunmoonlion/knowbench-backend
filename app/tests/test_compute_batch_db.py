"""Batch receipts use real PostgreSQL transactions; no model or live data."""

import json
import uuid

import pytest
from test_compute_batch import batch
from test_knowbot_compute import Reader, fact
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.acceptance import Verdict
from app.application.workbench.acceptance_numbers import judge_numbers
from app.application.workbench.knowbot_compute import COMPUTE_EVENT, KnowbotCompute
from app.application.workbench.ledger import Ledger
from app.application.workbench.records import ProjectRecords
from app.domain.workbench.errors import ComputeRefused, NotFound, RecordsRefused
from app.domain.workbench.packs import AcceptanceRule
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


async def new_task(db, *, owner=OWNER):
    _, _, sid = await seed(db, owner=owner, root=f"/home/u/research/{uuid.uuid4()}")
    async with db() as session:
        return (
            await Ledger(WorkbenchRepository(session)).handover(
                handover(sid, key=f"compute-{sid}"), owner_actor_id=owner
            )
        )["task_id"]


async def receipts(db, tid):
    async with db() as session:
        return await WorkbenchRepository(session).list_task_events(
            tid, types=(COMPUTE_EVENT,)
        )


async def test_batch_persists_ordinary_receipts_accepted_after_new_connection(db):
    tid = await new_task(db)
    async with db() as session:
        result = await KnowbotCompute(
            ProjectRecords(WorkbenchRepository(session), owner_actor_id=OWNER), Reader()
        ).compute_batch(tid, batch())
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        rows = [
            {"value": row["value"], "compute_id": row["id"]}
            for row in result["results"]
        ]
        verdict = await judge_numbers(
            Verdict(ok=True, parsed={"rows": rows}),
            (AcceptanceRule(kind="computed", path="rows", tolerance=0),),
            repo=repo,
            task=task,
        )
        assert verdict.ok, verdict.checks
        assert "batch:" not in json.dumps(
            await repo.list_task_events(tid, types=(COMPUTE_EVENT,)), default=str
        )
    assert len(await receipts(db, tid)) == 3


@pytest.mark.parametrize("failure", ["second_write", "output_limit"])
async def test_batch_transaction_rolls_back_every_receipt(db, monkeypatch, failure):
    tid = await new_task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        append = repo.append_event
        writes = 0

        async def failing_append(**kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise RuntimeError("simulated storage failure")
            return await append(**kwargs)

        if failure == "second_write":
            monkeypatch.setattr(repo, "append_event", failing_append)
            expected = RuntimeError
        else:
            monkeypatch.setattr(
                "app.application.workbench.knowbot_compute.TOOL_RESULT_MAX_BYTES", 100
            )
            expected = ComputeRefused
        with pytest.raises(expected):
            await KnowbotCompute(
                ProjectRecords(repo, owner_actor_id=OWNER), Reader()
            ).compute_batch(tid, batch())
    assert await receipts(db, tid) == []


async def test_stop_during_fact_read_refuses_batch_before_persisting(db):
    tid = await new_task(db)

    class StopReader(Reader):
        async def facts(self, repo, task, refs):
            async with db() as session:
                await Ledger(WorkbenchRepository(session)).transition(
                    tid, TaskState.CANCELLED
                )
            return await super().facts(repo, task, refs)

    async with db() as session:
        tool = KnowbotCompute(
            ProjectRecords(WorkbenchRepository(session), owner_actor_id=OWNER),
            StopReader(),
        )
        with pytest.raises(RecordsRefused):
            await tool.compute_batch(tid, batch())
    assert await receipts(db, tid) == []


async def test_batch_cannot_use_another_task_receipt_or_another_owners_task(db):
    first, second = await new_task(db), await new_task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        tool = KnowbotCompute(ProjectRecords(repo, owner_actor_id=OWNER), Reader())
        result = await tool.compute(first, "source", "x", {"x": fact(1)})
        with pytest.raises(ComputeRefused, match="no earlier computation"):
            await tool.compute_batch(
                second,
                [
                    {
                        "key": "x",
                        "name": "reuse",
                        "expression": "x",
                        "inputs": {"x": result["id"]},
                    }
                ],
            )
        outsider = KnowbotCompute(
            ProjectRecords(repo, owner_actor_id=str(uuid.uuid4())), Reader()
        )
        with pytest.raises(NotFound):
            await outsider.compute_batch(first, batch())
    assert await receipts(db, second) == []
    assert len(await receipts(db, first)) == 1
