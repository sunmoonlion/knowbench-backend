"""A missing internal MCP route must not become a cached partial tool catalog."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_knowbase_client import client

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.workbench.data_relay import RELAYED, DataRelay, _tools_cache
from app.application.workbench.tokens import TokenIssuer, generate_private_key_pem
from app.interfaces.mcp.workbench_mcp import handle

ISSUER = TokenIssuer(generate_private_key_pem())
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def catalog():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": name,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"run": {"type": "string"}},
                        "required": ["run"],
                    },
                }
                for name in sorted(RELAYED)
            ]
        },
    }


@pytest.fixture(autouse=True)
def clear_catalog():
    _tools_cache.clear()
    yield
    _tools_cache.clear()


def relay_for(kb, *, subject="alice", sandbox="sandbox-a", issuer=ISSUER):
    records = SimpleNamespace(
        owner=subject,
        repo=SimpleNamespace(subject_of_actor=AsyncMock(return_value=subject)),
    )
    return DataRelay(records, kb, issuer, sandbox=sandbox)


async def listed(relay):
    return await handle(relay.records, LIST, owner="alice", per_minute=60, relay=relay)


async def test_missing_route_is_an_error_and_recovers_without_cache_expiry(caplog):
    response = httpx.Response(404, text="404 page not found\n")
    kb, seen, oidc = client(lambda _: response)
    relay = relay_for(kb)
    failed = await listed(relay)
    assert failed["error"]["code"] == -32003
    assert "result" not in failed
    assert "does not mean the task has no data" in failed["error"]["message"]
    assert not _tools_cache
    assert "status=404" in caplog.text
    assert "404 page not found" not in caplog.text
    assert oidc.calls == []  # Data MCP uses its own JWT, not a service token.
    assert seen[0].url.path == "/api/mcp/data"
    assert seen[0].headers["authorization"].startswith("Bearer ")
    assert seen[0].headers["authorization"] not in caplog.text
    response = httpx.Response(200, json=catalog())
    restored = await listed(relay)
    specs = {tool["name"]: tool for tool in restored["result"]["tools"]}
    assert RELAYED <= specs.keys()
    assert {"list_inputs", "read_input", "query_facts"} <= specs.keys()
    assert specs["list_inputs"]["inputSchema"]["required"] == ["task"]
    assert "run" not in specs["list_inputs"]["inputSchema"]["properties"]
    assert len(seen) == 2
    await listed(relay)
    assert len(seen) == 2


def bad_catalogs():
    complete = catalog()
    partial = copy.deepcopy(complete)
    partial["result"]["tools"].pop()
    duplicate = copy.deepcopy(complete)
    duplicate["result"]["tools"].append(duplicate["result"]["tools"][0])
    invalid = copy.deepcopy(complete)
    invalid["result"]["tools"][0]["inputSchema"] = []
    return [
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "denied"}},
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        partial,
        duplicate,
        invalid,
    ]


@pytest.mark.parametrize("bad", bad_catalogs())
async def test_invalid_and_partial_catalogs_are_not_cached(bad):
    response = httpx.Response(200, json=bad)
    kb, seen, _ = client(lambda _: response)
    relay = relay_for(kb)
    assert "error" in await listed(relay)
    assert not _tools_cache
    response = httpx.Response(200, json=catalog())
    assert "result" in await listed(relay)
    assert len(seen) == 2


async def test_cached_catalog_is_scoped_copied_and_cannot_bypass_identity():
    kb, seen, _ = client(lambda _: httpx.Response(200, json=catalog()))
    alice = relay_for(kb)
    first = await alice.tools()
    first[0]["inputSchema"]["required"].append("caller_mutation")
    assert "caller_mutation" not in (await alice.tools())[0]["inputSchema"]["required"]
    assert len(seen) == 1
    await relay_for(kb, subject="bob").tools()
    await relay_for(kb, sandbox="sandbox-b").tools()
    await relay_for(kb, issuer=TokenIssuer(generate_private_key_pem())).tools()
    assert len(seen) == 4
    other, other_seen, _ = client(lambda _: httpx.Response(404, text="not found"))
    assert "error" in await listed(relay_for(other))
    assert len(other_seen) == 1
    alice.repo.subject_of_actor.return_value = None
    assert "error" in await listed(alice)


async def test_deliberately_unconfigured_installation_keeps_project_tools():
    kb, seen, _ = client(
        lambda _: pytest.fail("must not send"), KNOWBASE_INTERNAL_URL=""
    )
    result = await listed(relay_for(kb))
    names = {tool["name"] for tool in result["result"]["tools"]}
    assert "compute" in names and not names & RELAYED
    assert seen == []


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500, 502, 202, 204])
async def test_http_failures_are_not_results_or_silently_replayed(status):
    kb, seen, _ = client(
        lambda _: httpx.Response(status, text="private upstream details")
    )
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.data_mcp("test-only-token", LIST)
    assert caught.value.code == "knowbase_mcp_http_error"
    assert "private upstream details" not in str(caught.value)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "result": []},
    ],
)
async def test_invalid_json_rpc_envelopes_are_rejected(body):
    kb, _, _ = client(lambda _: httpx.Response(200, json=body))
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.data_mcp("test-only-token", LIST)
    assert caught.value.code == "knowbase_mcp_invalid_response"


async def test_non_json_success_is_rejected_but_notification_202_is_allowed():
    kb, _, _ = client(lambda _: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ServiceUnavailableError) as caught:
        await kb.data_mcp("test-only-token", LIST)
    assert caught.value.code == "knowbase_mcp_invalid_response"
    kb, _, _ = client(lambda _: httpx.Response(202))
    assert (
        await kb.data_mcp(
            "test-only-token",
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        == {}
    )


async def test_timeout_is_reported_without_poisoning_catalog_or_replaying():
    def timeout(request):
        raise httpx.ConnectTimeout("upstream unavailable", request=request)

    kb, seen, _ = client(timeout)
    assert "error" in await listed(relay_for(kb))
    assert not _tools_cache and len(seen) == 1
