"""A restarted or concurrent reconciler settles the original root budget exactly once."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from test_formal_script_execution_db import script_setup, status
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench import script_budget
from app.domain.workbench.errors import IdempotencyConflict
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_root_script_reservation_and_late_reconciliation(db):
    req, factory, conductor, _ = await script_setup(db)
    await conductor.advance(str(req.run_id))
    action = (await status(db, req, factory))["actions"][0]
    call_id = action["call_id"]
    quote = action["subject"]["script_quote"]
    maximum = Decimal(quote["maximum"])
    async with db() as session:
        repo = WorkbenchRepository(session)
        call = await repo.capability_call(OWNER, call_id)
        # Start at the dispatch boundary to race the first two reservations.
        async with repo.transaction():
            await repo.update_call(OWNER, call_id, "dispatching", call["detail"])

    async def reserve():
        async with db() as session:
            await script_budget.reserve(
                WorkbenchRepository(session), OWNER, call_id, quote
            )

    await asyncio.gather(reserve(), reserve())
    evidence = {"run_id": call_id, "receipt_id": str(uuid4())}

    async def settle(amount):
        async with db() as session:
            await script_budget.settle(
                WorkbenchRepository(session),
                OWNER,
                call_id,
                amount=Decimal(amount),
                evidence=evidence,
            )

    with pytest.raises(IdempotencyConflict):
        await settle(maximum + Decimal("0.1"))
    await asyncio.gather(settle("0.12"), settle("0.12"))
    with pytest.raises(IdempotencyConflict):
        await settle("0.13")
    async with db() as session:
        repo = WorkbenchRepository(session)
        entries = await repo.script_budget_entries(call_id)
        assert {k: v["amount"] for k, v in entries.items()} == {
            "reserve": maximum,
            "consume": Decimal("0.12"),
            "release": maximum - Decimal("0.12"),
        }
        task = await repo.get_task(str(call["task_id"]))
        assert Decimal(task["budget"]["used"]) == Decimal("0.12")
        assert Decimal(task["budget"]["reserved"]) == 0

        import importlib.util

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from test_workbench_ledger_db import ROOT

        spec = importlib.util.spec_from_file_location(
            "script_meter_migration",
            ROOT / "alembic/versions/20261120_0049_script_metering.py",
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        def round_trip(conn):
            with Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
                migration.upgrade()

        async with repo.transaction():
            await repo.ledger_append(
                task_id=str(call["task_id"]),
                attempt_id=None,
                entry="topup",
                amount=Decimal("1"),
                note="script-cost/v1:an ordinary historical note",
            )
            connection = await session.connection()
            await connection.run_sync(round_trip)
        assert await repo.script_budget_entries(call_id) == entries
