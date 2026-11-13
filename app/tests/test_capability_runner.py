"""A repeated command must not rotate an active turn's capability credentials."""

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
    factory, stores = Mock(), Mock()
    runner = SimpleNamespace(capabilities=factory, stores=stores)
    link = SandboxLink(runner, {"id": "sandbox-fixture"})
    client = SimpleNamespace(request=AsyncMock(return_value=state))
    monkeypatch.setattr(link, "ensure_connected", AsyncMock(return_value=client))
    link.capability_scopes["thread-fixture"] = "existing-scope"
    link.capability_config["thread-fixture"] = {"existing": "config"}
    if remembered_active:
        link.active_turns["thread-fixture"] = "in-progress-turn"

    with pytest.raises(RuntimeError, match="active or unknown"):
        await link.prepare_capabilities("thread-fixture", expert=False)

    factory.assert_not_called()
    stores.assert_not_called()
    assert link.capability_scopes["thread-fixture"] == "existing-scope"
    assert link.capability_config["thread-fixture"] == {"existing": "config"}
    client.request.assert_awaited_once_with(
        "thread/read",
        {"threadId": "thread-fixture", "includeTurns": False},
        timeout=10,
    )
