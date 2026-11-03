"""A missing runtime tool catalog cannot be papered over by model prose."""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.workbench import runner as runtime
from app.application.workbench import runtime_tools
from app.application.workbench.runtime_tools import unavailable_data_tools


def catalog(names, **extra):
    return {
        "data": [
            {
                "name": "sunmoon_workbench",
                "tools": {
                    "qualified_" + n: {"name": n, "inputSchema": {"type": "object"}}
                    for n in names
                },
                **extra,
            }
        ],
        "nextCursor": None,
    }


async def test_catalog_must_be_for_exact_thread_and_server():
    client = SimpleNamespace(request=AsyncMock(return_value=catalog(["read_input"])))
    assert (
        await unavailable_data_tools(client, "thread", frozenset({"read_input"}))
        is None
    )
    args = client.request.call_args
    assert args.args[0] == "mcpServerStatus/list"
    assert args.args[1]["threadId"] == "thread"
    assert args.kwargs["timeout"] == 10
    wrong = catalog(["read_input"])
    wrong["data"][0]["name"] = "unrelated"
    client.request.return_value = wrong
    assert await unavailable_data_tools(client, "thread", frozenset({"read_input"}))


@pytest.mark.parametrize(
    "reply",
    [
        {},
        None,
        {"data": []},
        catalog([]),
        catalog(["read_input"], toolsError="secret upstream body"),
        catalog(["read_input"], runtimeStatus="failed"),
        catalog(["read_input"], runtimeStatus="starting"),
        {"data": [{"name": "sunmoon_workbench", "tools": ["read_input"]}]},
    ],
)
async def test_missing_failed_or_invalid_inventory_blocks(reply):
    client = SimpleNamespace(request=AsyncMock(return_value=reply))
    result = await unavailable_data_tools(client, "thread", frozenset({"read_input"}))
    assert result and "secret" not in result


async def test_empty_requirements_do_not_probe_and_failures_do_not_leak():
    client = SimpleNamespace(request=AsyncMock(side_effect=TimeoutError("secret URL")))
    assert await unavailable_data_tools(client, "thread", frozenset()) is None
    client.request.assert_not_called()
    result = await unavailable_data_tools(client, "thread", frozenset({"read_input"}))
    assert result and "secret" not in result


async def test_pagination_is_bounded_and_can_find_later_server():
    client = SimpleNamespace(
        request=AsyncMock(
            side_effect=[{"data": [], "nextCursor": "second"}, catalog(["query_facts"])]
        )
    )
    assert await unavailable_data_tools(client, "t", frozenset({"query_facts"})) is None
    assert client.request.call_args.args[1]["cursor"] == "second"
    client.request = AsyncMock(return_value={"data": [], "nextCursor": "cycle"})
    assert await unavailable_data_tools(client, "t", frozenset({"query_facts"}))
    assert client.request.call_count == 2


async def test_only_active_snapshot_step_declares_data_tools(monkeypatch):
    repo = SimpleNamespace(
        get_session_by_thread=AsyncMock(
            return_value={"wheel": "advisor", "active_task_id": "task"}
        ),
        get_task=AsyncMock(
            return_value={
                "pack_source": "knowmart",
                "state": "RUNNING",
                "active_attempt_id": "a",
                "current_step": 0,
            }
        ),
        get_attempt=AsyncMock(return_value={"step_id": "inventory"}),
    )
    step = SimpleNamespace(step_id="inventory", tools=["read_input", "compute"])
    monkeypatch.setattr(
        runtime_tools,
        "execution_for_task",
        AsyncMock(
            return_value=SimpleNamespace(pack=SimpleNamespace(step=lambda i: step))
        ),
    )
    assert await runtime_tools.declared_data_tools(repo, "t") == {"read_input"}
    repo.get_task.return_value["pack_source"] = "builtin"
    assert not await runtime_tools.declared_data_tools(repo, "t")
    repo.get_task.return_value["pack_source"] = "knowmart"
    repo.get_attempt.return_value["step_id"] = runtime_tools.PLAN_STEP
    assert not await runtime_tools.declared_data_tools(repo, "t")
    repo.get_attempt.return_value["step_id"] = "data-plan"
    assert await runtime_tools.declared_data_tools(repo, "t") == {"describe_items"}


async def test_missing_tools_prevents_turn_start_and_token_spend(monkeypatch):
    @asynccontextmanager
    async def stores():
        yield SimpleNamespace(get_session_by_thread=AsyncMock(return_value=None))

    client = SimpleNamespace(request=AsyncMock(return_value=catalog([])))
    link = runtime.SandboxLink(
        SimpleNamespace(
            stores=stores,
            turn_plan=AsyncMock(return_value=SimpleNamespace(settings={})),
        ),
        {"id": "sandbox"},
    )
    monkeypatch.setattr(link, "ensure_connected", AsyncMock(return_value=client))
    monkeypatch.setattr(link, "ensure_thread", AsyncMock())
    monkeypatch.setattr(link, "announce", AsyncMock())
    monkeypatch.setattr(link, "configure_search_scope", AsyncMock())
    monkeypatch.setattr(runtime, "capture_attempt", AsyncMock(return_value=None))
    monkeypatch.setattr(
        runtime,
        "declared_data_tools",
        AsyncMock(return_value=frozenset({"query_facts"})),
    )
    result = await link.run_turn("t", "read actual data")
    link.configure_search_scope.assert_awaited_once_with("t", restricted=True)
    assert result.platform_failure and result.error
    assert result.turn_id is None and result.final_text is None
    assert result.cost == Decimal("0") and result.tokens == {}
    assert [c.args[0] for c in client.request.call_args_list] == [
        "mcpServerStatus/list"
    ]
