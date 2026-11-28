"""User turn acceptance and queueing are atomic, retries share the original receipt."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import OWNER, seed
from test_workbench_ledger_db import db as db

from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import workbench_routes as routes


@pytest.mark.parametrize("retry", [False, True])
async def test_acceptance_and_queue(db, monkeypatch, retry):
    _, _, sid = await seed(db)
    body = routes.StartTurn(text="比较两份测试记录", request_id="same-first-turn")
    monkeypatch.setattr(routes, "ensure_data_session", AsyncMock())
    monkeypatch.setattr(routes, "knowbase_v2", lambda: None)
    monkeypatch.setattr(routes, "publish_commands_wakeup", AsyncMock())
    if not retry:
        monkeypatch.setattr(
            WorkbenchRepository,
            "enqueue_command",
            AsyncMock(side_effect=RuntimeError("queue unavailable")),
        )

    async def submit():
        async with db() as session:
            return await routes.start_turn(
                sid, body, SimpleNamespace(actor_id=OWNER), session, AsyncMock()
            )

    if retry:
        first, second = await asyncio.gather(submit(), submit())
        assert first == second
        from app.application.errors.exceptions import AppException

        body = routes.StartTurn(text="different request", request_id=body.request_id)
        with pytest.raises(AppException) as changed:
            await submit()
        assert changed.value.status_code == 409
    else:
        with pytest.raises(RuntimeError, match="queue unavailable"):
            await submit()
    async with db() as session:
        events = await WorkbenchRepository(session).list_record_events(
            session_id=sid, types=("turn/requested",)
        )
        commands = await session.scalar(
            text(
                "select count(*) from workbench_commands where session_id=:s and kind='turn.start'"
            ),
            {"s": sid},
        )
        assert (len(events), commands) == ((1, 1) if retry else (0, 0))
