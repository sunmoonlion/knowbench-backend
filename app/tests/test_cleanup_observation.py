"""Operator-facing backlog and transition signals for the execution-unit collectors."""

import asyncio
import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.application.workbench import cleanup_observation as observation
from app.interfaces.http.admin import diagnostics
from app.main import app


async def test_overdue_survives_observation_failure_and_recovers_once(
    caplog, monkeypatch
):
    def samples(age):
        return {"metered_scripts": {"pending": int(bool(age)), "oldest_seconds": age}}

    read = AsyncMock(
        side_effect=[
            samples(299),
            samples(300),
            RuntimeError(),
            RuntimeError(),
            samples(315),
            samples(0),
            asyncio.CancelledError(),
        ]
    )
    repo = SimpleNamespace(cleanup_backlog=read)
    monkeypatch.setattr(
        observation,
        "asyncio",
        SimpleNamespace(sleep=AsyncMock(), timeout=asyncio.timeout),
    )
    with caplog.at_level(logging.INFO, logger=observation.__name__):
        with pytest.raises(asyncio.CancelledError):
            await observation.watch(lambda: nullcontext(repo))
    assert caplog.messages == [
        "workbench_cleanup_overdue queue=metered_scripts pending=1 oldest_seconds=300",
        "workbench_cleanup_observation_unavailable",
        "workbench_cleanup_observation_restored",
        "workbench_cleanup_recovered queue=metered_scripts pending=0 oldest_seconds=0",
    ]


async def test_cleanup_diagnostics_requires_operations_access_and_reports_read_failure(
    monkeypatch,
):
    monkeypatch.setitem(
        app.dependency_overrides, diagnostics.get_db_session, lambda: None
    )
    sample = {"instruction_units": {"pending": 1, "oldest_seconds": 305}}
    read = AsyncMock(side_effect=[sample, RuntimeError("private connection detail")])
    monkeypatch.setattr(
        diagnostics,
        "WorkbenchRepository",
        lambda _: SimpleNamespace(cleanup_backlog=read),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        url = "/api/admin/v1/diagnostics/tasks/cleanup"
        denied = await client.get(url)
        assert denied.status_code in {401, 403} and not read.await_count
        monkeypatch.setitem(
            app.dependency_overrides, diagnostics.require_knowbench_admin, lambda: "ops"
        )
        result = await client.get(url)
        assert (
            result.status_code == 200 and "no-store" in result.headers["Cache-Control"]
        )
        assert result.json() == {
            "alert_after_seconds": 300,
            "queues": {
                "instruction_units": {
                    "pending": 1,
                    "oldest_seconds": 305,
                    "alert": True,
                }
            },
        }
        failure = await client.get(url)
        assert (
            failure.status_code == 503
            and "private connection detail" not in failure.text
        )
