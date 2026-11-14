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


async def test_old_revocation_cannot_interrupt_a_replacement_attempt():
    attempt = AsyncMock(side_effect=AssertionError("must not inspect an old attempt"))

    @asynccontextmanager
    async def stores():
        yield SimpleNamespace(
            get_task=AsyncMock(return_value={"active_attempt_id": "replacement"}),
            get_attempt=attempt,
        )

    link = SandboxLink(SimpleNamespace(stores=stores), {"id": "sandbox"})
    assert await link.method_capability_state(("task", "revoked-attempt")) == (
        False,
        False,
    )
    attempt.assert_not_called()


@pytest.mark.parametrize("status", ["active", "notLoaded", "unknown", None])
async def test_paginated_or_unknown_thread_never_confirms_stop(status, monkeypatch):
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    thread = {"id": "thread", "turns": []}
    if status is not None:
        thread["status"] = {"type": status}
    client = SimpleNamespace(request=AsyncMock(return_value={"thread": thread}))
    monkeypatch.setattr(link, "ensure_connected", AsyncMock(return_value=client))
    monkeypatch.setattr(link, "remember_model", Mock())
    assert await link.stop_and_confirm("thread", 0.001) == (None, False)
    assert any(c.args[0] == "thread/read" for c in client.request.await_args_list)
    assert not any(
        c.args[0] == "turn/interrupt" for c in client.request.await_args_list
    )


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"thread": {}},
        {"thread": {"id": "thread", "turns": []}},
        {"thread": {"id": "other", "status": {"type": "idle"}}},
    ],
)
async def test_missing_turn_and_unknown_read_are_not_terminal(reply, monkeypatch):
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    client = SimpleNamespace(request=AsyncMock(return_value=reply))
    monkeypatch.setattr(link, "ensure_connected", AsyncMock(return_value=client))
    assert await link.turn_running("thread", "turn") is None


@pytest.mark.parametrize(
    "last,turn_id,thread_id,known",
    [
        (
            {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            "turn",
            "thread",
            True,
        ),
        ({}, "turn", "thread", False),
        (
            {"inputTokens": "0", "outputTokens": 0, "totalTokens": 0},
            "turn",
            "thread",
            False,
        ),
        (
            {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            "other",
            "thread",
            False,
        ),
        (
            {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            "turn",
            "other",
            False,
        ),
    ],
)
def test_only_explicit_exact_turn_zero_usage_is_known(last, turn_id, thread_id, known):
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    waiter = {"thread_id": "thread", "reported": False}
    link.turn_waiters["turn"] = waiter
    link.priced(
        "thread/tokenUsage/updated",
        {
            "threadId": thread_id,
            "turnId": turn_id,
            "tokenUsage": {"last": last},
        },
    )
    assert waiter["reported"] is known


@pytest.mark.parametrize("idle_before", [False, True])
async def test_finished_turn_interrupt_error_still_requires_idle_metadata(
    idle_before, monkeypatch
):
    link = SandboxLink(SimpleNamespace(), {"id": "sandbox"})
    reads = 0

    async def request(method, params, **kwargs):
        nonlocal reads
        if method == "turn/interrupt":
            raise RuntimeError("turn already finished")
        if method == "thread/read":
            reads += 1
        status = "idle" if idle_before or reads > 1 else "active"
        return {"thread": {"id": "thread", "turns": [], "status": {"type": status}}}

    client = SimpleNamespace(request=AsyncMock(side_effect=request))
    monkeypatch.setattr(link, "ensure_connected", AsyncMock(return_value=client))
    monkeypatch.setattr(link, "remember_model", Mock())
    assert await link.stop_and_confirm("thread", 0.01, turn_id="persisted") == (
        "persisted",
        True,
    )
    interrupts = [
        c for c in client.request.await_args_list if c.args[0] == "turn/interrupt"
    ]
    assert len(interrupts) == (0 if idle_before else 1)
    assert reads == (1 if idle_before else 2)
