"""Formal work must branch before any model session or recovery operation."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.workbench.formal_execution import FormalExecution
from app.application.workbench.runner import Runner
from app.domain.workbench.formal_execution import FORMAL_RUNTIME


def runner_for(task):
    @asynccontextmanager
    async def stores():
        yield SimpleNamespace(
            get_session=AsyncMock(
                return_value={
                    "id": "session",
                    "owner_actor_id": "owner",
                    "sandbox_id": "sandbox",
                }
            ),
            get_task=AsyncMock(return_value=task),
        )

    runner = object.__new__(Runner)
    runner.stores, runner.driving, runner.capabilities = stores, {}, None
    runner.link_for = AsyncMock(
        side_effect=AssertionError("Formal work cannot acquire a Codex link")
    )
    return runner


@pytest.mark.parametrize(
    "identity",
    [
        {"pack_source": "formal-test"},
        {"pack_snapshot": {"runtime_contract": FORMAL_RUNTIME}},
    ],
)
@pytest.mark.parametrize("command", ["task.drive", "task.reconnect", "turn.interrupt"])
async def test_host_command_branches_before_model_link(identity, command, monkeypatch):
    task = {"id": "task", "session_id": "session", **identity}
    runner = runner_for(task)
    drive = AsyncMock(return_value="WAITING")
    monkeypatch.setattr(FormalExecution, "drive", drive)
    await runner.handle(
        {"kind": command, "payload": {"task_id": "task"}, "session_id": "session"}
    )
    await runner.wait_drivers()
    runner.link_for.assert_not_called()
    assert drive.await_count == (1 if command == "task.drive" else 0)


async def test_reconciliation_uses_authoritative_host_task_not_partial_projection(
    monkeypatch,
):
    runner = runner_for({"id": "task", "pack_source": "formal-test"})
    drive = AsyncMock(return_value="WAITING")
    monkeypatch.setattr(FormalExecution, "drive", drive)
    await runner._settle({"id": "task", "state": "WAITING"})
    drive.assert_awaited_once_with("task")
    runner.link_for.assert_not_called()


async def test_invalid_host_snapshot_never_falls_back_to_model_crash_handler(
    monkeypatch,
):
    runner = runner_for(
        {"id": "task", "session_id": "session", "pack_source": "formal-test"}
    )
    monkeypatch.setattr(
        FormalExecution,
        "drive",
        AsyncMock(side_effect=ValueError("invalid immutable snapshot")),
    )
    runner._crashed = AsyncMock(side_effect=AssertionError("No model crash settlement"))
    await runner.handle(
        {"kind": "task.drive", "payload": {"task_id": "task"}, "session_id": "session"}
    )
    await runner.wait_drivers()
    runner.link_for.assert_not_called()
    runner._crashed.assert_not_called()
