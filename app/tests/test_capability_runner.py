"""A repeated command must not rotate an active turn's capability credentials."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.workbench.runner import SandboxLink


@pytest.mark.parametrize(
    ("state", "remembered_active"),
    [
        ({"thread": {"status": {"type": "active"}}}, False),
        ({"thread": {"status": {"type": "notLoaded"}}}, False),
        ({"thread": {"status": {"type": "idle"}}}, True),
        ({"thread": {}}, False),
    ],
)
async def test_busy_or_unknown_thread_preserves_current_scope(
    state, remembered_active, monkeypatch
):
    factory = Mock()

    @asynccontextmanager
    async def stores():
        yield SimpleNamespace(
            get_session_by_thread=AsyncMock(
                return_value={"id": "session", "owner_actor_id": "owner"}
            ),
            capability_context_exists=AsyncMock(return_value=True),
        )

    runner = SimpleNamespace(capabilities=factory, stores=stores)
    link = SandboxLink(runner, {"id": "sandbox-fixture"})
    client = SimpleNamespace(request=AsyncMock(return_value=state))
    monkeypatch.setattr(link, "ensure_connected", AsyncMock(return_value=client))
    link.capability_scopes["thread-fixture"] = "existing-scope"
    link.capability_config["thread-fixture"] = {"existing": "config"}
    if remembered_active:
        link.active_turns["thread-fixture"] = "in-progress-turn"

    with pytest.raises(RuntimeError, match="active or unknown"):
        await link.prepare_capabilities("thread-fixture", scene="conversation")

    factory.assert_not_called()
    assert link.capability_scopes["thread-fixture"] == "existing-scope"
    assert link.capability_config["thread-fixture"] == {"existing": "config"}
    client.request.assert_awaited_once_with(
        "thread/read",
        {"threadId": "thread-fixture", "includeTurns": False},
        timeout=10,
    )


async def test_empty_conversation_does_not_read_thread_or_prepare_scope(monkeypatch):
    factory = Mock()

    @asynccontextmanager
    async def stores():
        yield SimpleNamespace(
            get_session_by_thread=AsyncMock(
                return_value={"id": "session", "owner_actor_id": "owner"}
            ),
            capability_context_exists=AsyncMock(return_value=False),
        )

    link = SandboxLink(
        SimpleNamespace(capabilities=factory, stores=stores), {"id": "sandbox"}
    )
    connect = AsyncMock(
        side_effect=AssertionError("No capability protocol work needed")
    )
    monkeypatch.setattr(link, "ensure_connected", connect)
    assert await link.prepare_capabilities("thread", scene="conversation") == []
    connect.assert_not_called()
    factory.assert_not_called()
