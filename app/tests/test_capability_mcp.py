"""Transport limits and schema isolation, without external network or credentials."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.tool_schema import schema_validator
from app.infrastructure.workbench import capability_mcp as mcp


@pytest.mark.parametrize(
    "ref",
    ["https://127.0.0.1/secret", "file:///etc/passwd", "//metadata/", "relative.json"],
)
def test_schema_does_not_fetch_external_refs(ref):
    with pytest.raises(CapabilityError):
        schema_validator({"type": "object", "properties": {"value": {"$ref": ref}}})


def test_local_schema_refs_work_and_bad_or_recursive_schemas_fail_closed():
    validator = schema_validator(
        {
            "$defs": {"value": {"type": "integer"}},
            "properties": {"x": {"$ref": "#/$defs/value"}},
        }
    )
    assert validator.is_valid({"x": 3}) and not validator.is_valid({"x": "3"})
    with pytest.raises(CapabilityError):
        schema_validator({"type": "not-a-type"})
    with pytest.raises(CapabilityError):
        schema_validator({"$id": "https://internal/"})
    deep = {}
    for _ in range(35):
        deep = {"items": deep}
    with pytest.raises(CapabilityError):
        schema_validator(deep)


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "::ffff:127.0.0.1"]
)
async def test_private_dns_rejected(monkeypatch, ip):
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(None, None, None, None, (ip, 443))]),
    )
    with pytest.raises(CapabilityError):
        await mcp.public_address("https://test.example/mcp")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "https://a:b@example.com/mcp",
        "https://example.com:123/mcp",
        "https://example.com/mcp?key=x",
        "https://example.com:bad/mcp",
    ],
)
async def test_invalid_endpoint_rejected(url):
    with pytest.raises(CapabilityError):
        await mcp.public_address(url)


async def test_streaming_reply_returns_before_connection_closes_and_pins_dns(
    monkeypatch,
):
    seen = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message\r\ndata: {"jsonrpc":"2.0","id":3,"result":{"content":[]}}\r\n\r\n'
            raise AssertionError("Must not wait for EOF after response event")

    def handler(request):
        seen.append(request)
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-test"},
                json={"id": message["id"], "result": {"protocolVersion": "2025-06-18"}},
            )
        if message["method"].startswith("notifications/"):
            return httpx.Response(202)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        mcp,
        "public_address",
        AsyncMock(return_value=("93.184.216.34", "tools.example")),
    )
    monkeypatch.setattr(
        mcp.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    remote = mcp.RemoteMcp(("https://tools.example/mcp",))
    assert await remote.call("https://tools.example/mcp", "test-token", "send", {}) == {
        "content": []
    }
    assert all(str(r.url) == "https://93.184.216.34/mcp" for r in seen)
    assert all(
        r.headers["Host"] == "tools.example"
        and r.extensions["sni_hostname"] == "tools.example"
        for r in seen
    )
    assert seen[-1].headers["Mcp-Session-Id"] == "session-test"
    assert seen[-1].headers["MCP-Protocol-Version"] == "2025-06-18"


async def test_unreviewed_endpoint_is_never_resolved(monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(mcp, "public_address", resolve)
    with pytest.raises(CapabilityError):
        await mcp.RemoteMcp().discover("https://tools.example/mcp", None)
    resolve.assert_not_called()


@pytest.mark.parametrize(
    "revoked", [False, True], ids=["unauthorized-response", "revoked-after-initialize"]
)
async def test_dispatch_rechecks_after_handshake_and_never_replays(
    monkeypatch,
    revoked,
):
    from app.domain.workbench.capabilities import CapabilityAuthOutcomeUnknown

    calls = []

    def handler(req):
        rpc = json.loads(req.content)
        calls.append(rpc["method"])
        if rpc["method"] == "notifications/initialized":
            return httpx.Response(204)
        if rpc["method"] == "tools/call":
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rpc["id"],
                "result": {"protocolVersion": "2025-06-18"},
            },
        )

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        mcp, "public_address", AsyncMock(return_value=("192.0.2.1", "tools.example"))
    )
    monkeypatch.setattr(
        mcp.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def before_send():
        assert calls == ["initialize", "notifications/initialized"]
        if revoked:
            raise CapabilityError("approval revoked")

    error = CapabilityError if revoked else CapabilityAuthOutcomeUnknown
    with pytest.raises(error):
        await mcp.RemoteMcp(("https://tools.example/mcp",)).call_checked(
            "https://tools.example/mcp", "test", "send", {}, before_send=before_send
        )
    assert calls.count("tools/call") == (0 if revoked else 1)
