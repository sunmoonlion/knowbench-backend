"""Ordinary dynamic author trial with a real ledger and a local test database."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_dynamic_incubation_db import admit, dynamic_request
from test_knowbot_test_runs_db import TrialAppServer, setup_test_computer
from test_workbench_ledger_db import db as db

from app.application.workbench import test_runs as runs
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.states import TASK_TERMINAL
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_retry_cancel_stops_the_existing_task(db, monkeypatch):
    fake = TrialAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        req = dynamic_request()
        await admit(db, req)
        await runs.KnowbotTestConductor(workbench_stores(db)).tick()
        async with db() as session:
            repo = WorkbenchRepository(session)
            (case,) = await repo.list_test_cases(req.run_id)
            task_id = str(case["task_id"])
            assert (await repo.get_task(task_id))["state"] not in TASK_TERMINAL
            with monkeypatch.context() as patch:
                patch.setattr(
                    runs,
                    "_stop_case_task",
                    AsyncMock(side_effect=RuntimeError("stop interrupted")),
                )
                with pytest.raises(RuntimeError, match="stop interrupted"):
                    await runs.KnowbotTestService(repo).cancel(req.run_id)
        async with db() as session:
            repo = WorkbenchRepository(session)
            assert (await runs.KnowbotTestService(repo).cancel(req.run_id))[
                "status"
            ] == "cancelled"
            task = await repo.get_task(task_id)
            assert (
                task["state"] in TASK_TERMINAL
                or task["cancel_requested_at"] is not None
            )
    finally:
        await fake.close()


async def test_identical_overlapping_admissions(db, monkeypatch):
    fake = TrialAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        req = dynamic_request()
        barrier = asyncio.Barrier(2)
        get_run = WorkbenchRepository.get_test_run

        async def simultaneous_read(repo, *args, **kwargs):
            row = await get_run(repo, *args, **kwargs)
            if row is None:
                await asyncio.wait_for(barrier.wait(), 5)
            return row

        monkeypatch.setattr(WorkbenchRepository, "get_test_run", simultaneous_read)
        replies = await asyncio.gather(
            admit(db, req), admit(db, req), return_exceptions=True
        )
        assert all(isinstance(x, dict) and x["status"] == "accepted" for x in replies)
        async with db() as session:
            assert len(
                await WorkbenchRepository(session).list_test_cases(req.run_id)
            ) == len(req.cases)
    finally:
        await fake.close()
