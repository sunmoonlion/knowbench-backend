"""Settle an invalid step after releasing its reader connection."""

from contextlib import asynccontextmanager

from formal_plan_support import advance, setup_plan
from test_formal_execution_db import approve
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_orchestration_execution import (
    FormalOrchestrationExecution,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_invalid_output_connection(db):
    ctx = await setup_plan(db)
    pending = await advance(db, ctx)
    ctx.remote.invalid = True
    await approve(db, ctx.request, ctx.factory, pending)
    open_sessions = []
    overlaps = []

    @asynccontextmanager
    async def stores():
        if open_sessions:
            overlaps.append(open_sessions[-1].in_transaction())
        async with db() as session:
            open_sessions.append(session)
            try:
                yield WorkbenchRepository(session)
            finally:
                open_sessions.pop()

    tid = pending["cases"][0]["task_id"]
    result = await FormalOrchestrationExecution(stores, ctx.factory).drive(tid)
    assert result == "FAILED"
    assert not any(overlaps)
