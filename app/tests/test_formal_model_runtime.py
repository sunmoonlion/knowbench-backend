"""Closed candidate scopes and bounded model execution, without model calls."""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.workbench.runner import SandboxLink
from app.application.workbench.runtime_tools import verify_probe_inventory
from app.domain.workbench.formal_model import PROBE_SERVER, PROBE_TOOL, FormalProbeGuard


def candidate():
    return {
        "name": PROBE_SERVER,
        "runtimeStatus": "connected",
        "tools": {PROBE_TOOL: {"name": PROBE_TOOL, "inputSchema": {"type": "object"}}},
    }


def test_only_the_candidate_can_be_visible():
    verify_probe_inventory([candidate()])
    for rows in (
        [],
        [candidate(), candidate()],
        [{**candidate(), "resources": [{"uri": "secret"}]}],
        [candidate(), {"name": "other", "tools": {"read": {"name": "read"}}}],
        [{**candidate(), "toolsError": "offline"}],
        [{**candidate(), "tools": {}}],
    ):
        with pytest.raises(RuntimeError):
            verify_probe_inventory(rows)


def test_repeated_observation_is_not_a_second_invocation():
    guard = FormalProbeGuard(Decimal("1"))
    item = {
        "type": "mcpToolCall",
        "server": PROBE_SERVER,
        "tool": PROBE_TOOL,
        "id": "one",
    }
    guard.observe(item)
    guard.observe(item)
    assert guard.failure is None
    guard.observe({**item, "id": "two"})
    assert guard.failure == "method_call_limit"


@pytest.mark.parametrize(
    "kind",
    [
        "commandExecution",
        "webSearch",
        "fileChange",
        "collabAgentToolCall",
        "dynamicToolCall",
    ],
)
def test_native_tool_observation_fails_the_probe(kind):
    guard = FormalProbeGuard(Decimal("1"))
    guard.observe({"type": kind})
    assert guard.failure == "unexpected_tool"


@pytest.mark.parametrize(
    "cost,reason",
    [(None, "model_cost_unknown"), ("1", "budget_limit"), ("2", "budget_limit")],
)
def test_unknown_or_exhausted_cost_stops_the_probe(cost, reason):
    guard = FormalProbeGuard(Decimal("1"))
    guard.usage(cost)
    assert guard.failure == reason


@pytest.mark.parametrize("method", ["question", "elicitation", "native_approval"])
async def test_probe_never_turns_native_questions_into_operation_approval(method):
    repo = AsyncMock()
    session = {"id": "session", "thread_id": "thread", "active_task_id": "task"}
    repo.get_session_by_thread.return_value = session
    repo.get_session.return_value = session
    repo.get_task.return_value = {
        "pack_source": "formal-test",
        "pack_snapshot": {"caller": "model"},
    }

    @asynccontextmanager
    async def stores():
        yield repo

    link = object.__new__(SandboxLink)
    link.runner = SimpleNamespace(stores=stores)
    link.client = AsyncMock()
    link.threads = {}
    link.sandbox_id = "sandbox"
    guard = FormalProbeGuard(Decimal("1"))
    link.turn_waiters = {"turn": {"thread_id": "thread", "probe_guard": guard}}
    if method == "question":
        await link._on_user_input(
            1,
            {
                "threadId": "thread",
                "questions": [
                    {"id": "q", "question": "Authorize a different operation?"}
                ],
            },
        )
        link.client.respond.assert_awaited_once_with(
            1, {"answers": {"q": {"answers": []}}}
        )
    elif method == "elicitation":
        await link._on_elicitation(
            1,
            {
                "threadId": "thread",
                "mode": "form",
                "requestedSchema": {
                    "properties": {"approved": {"type": "boolean", "default": True}}
                },
            },
        )
        link.client.respond.assert_awaited_once_with(1, {"action": "decline"})
    else:
        await link.on_server_request(
            1,
            "item/commandExecution/requestApproval",
            {"threadId": "thread", "command": "untrusted command"},
        )
        link.client.respond.assert_awaited_once_with(1, {"decision": "decline"})
    assert guard.failure is not None
    repo.insert_interaction.assert_not_awaited()


@pytest.mark.parametrize(
    "version",
    [
        None,
        {},
        {"userAgent": "client/0.155.1 (Linux)"},
        {"userAgent": "client/0.170.1 (Linux)"},
        {"userAgent": "client/0.170.00 (Linux)"},
    ],
)
async def test_probe_requires_exact_handshake_version_before_configuration(version):
    link = object.__new__(SandboxLink)
    client = AsyncMock()
    client.server_info = version or {}
    link.ensure_connected = AsyncMock(return_value=client)
    with pytest.raises(RuntimeError, match="version"):
        await link.configure_formal_probe("thread")
    client.request.assert_not_awaited()
