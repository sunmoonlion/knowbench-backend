"""Exercise crash boundaries and accepted handback identity with a local database."""

import asyncio
from copy import deepcopy

import pytest
import pytest_asyncio
from test_knowbot_incubations_db import (
    GOOD,
    HANDBACK,
    accept,
    cancel,
    request,
    status,
    the_task,
)
from test_knowbot_test_runs_db import runner_for, setup_test_computer
from test_workbench_ledger_db import db as db
from test_workbench_task_runtime_db import ScriptedAppServer, close, drive

from app.application.workbench import incubations
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.packs import INCUBATE
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest_asyncio.fixture
async def computer(db):
    fake = ScriptedAppServer([GOOD])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        yield incubations.IncubationConductor(workbench_stores(db))
    finally:
        await fake.close()


async def test_replayed_cancel_finishes_task_cleanup(db, computer, monkeypatch):
    await accept(db, request())
    await computer.tick()

    async def unavailable(*args, **kwargs):
        raise OSError("temporary interruption")

    with monkeypatch.context() as changes:
        changes.setattr(incubations, "cancel_task", unavailable)
        with pytest.raises(OSError):
            await cancel(db)
    assert (await cancel(db))["status"] == "cancelled"
    await computer.tick()
    assert (await the_task(db))["state"] == "CANCELLED"


async def test_cancel_as_starting_cannot_reopen_round(db, computer, monkeypatch):
    await accept(db, request())

    class CancellingFolders:
        async def ensure(self, **kwargs):
            assert (await cancel(db))["status"] == "cancelled"
            return True

    computer.folders = CancellingFolders()
    await computer.advance(request().incubation_id)
    report = await status(db)
    assert (report["status"], report["task_id"]) == ("cancelled", None)


async def test_concurrent_replay_returns_same_receipt(db, computer, monkeypatch):
    gate = asyncio.Barrier(2)
    get = WorkbenchRepository.get_incubation

    async def first_read(self, *args, **kwargs):
        row = await get(self, *args, **kwargs)
        if row is None:
            await asyncio.wait_for(gate.wait(), 5)
        return row

    monkeypatch.setattr(WorkbenchRepository, "get_incubation", first_read)
    a, b = await asyncio.gather(accept(db, request()), accept(db, request()))
    assert a == b


async def test_handback_reads_accepted_version(db, computer):
    await accept(db, request())
    await computer.tick()
    runner = runner_for(db)
    try:
        await drive(runner)
    finally:
        await close(runner)
    task_id = (await status(db))["task_id"]
    other = deepcopy(HANDBACK)
    other["gaps"][0]["question"] = "这条后写的问题没有经过该次验收"
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            appended = await repo.put_artifact(
                task_id=task_id,
                attempt_id=None,
                name=INCUBATE.workflow[0].output_artifact,
                kind="step",
                content=other,
            )
            assert appended["version"] > 1
    await computer.tick()
    assert (await status(db))["handback"] == HANDBACK


async def test_collector_preserves_rejection_reason(db, computer):
    from app.application.workbench.ledger import Ledger
    from app.domain.workbench.states import TaskState

    await accept(db, request())
    await computer.tick()
    task_id = (await status(db))["task_id"]
    reason = {"code": "workspace_missing", "message": "Author workspace unavailable"}
    async with db() as session:
        ledger = Ledger(WorkbenchRepository(session))
        await ledger.transition(task_id, TaskState.VALIDATING)
        await ledger.transition(task_id, TaskState.REJECTED, reason=reason)
    await computer.tick()
    assert reason["message"] in (await status(db))["error"]


async def test_replay_after_workspace_removed_returns_previous_receipt(db, computer):
    from test_knowbot_test_runs_db import ENV
    from test_workbench_ledger_db import OWNER

    await accept(db, request())
    previous = await cancel(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.set_environment_status(ENV, "offline")
            await repo.remove_environment(ENV, owner_actor_id=OWNER)
    assert await accept(db, request()) == previous
