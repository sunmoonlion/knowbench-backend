"""Exercise the production adapter with deterministic HTTP failures, not fake RPCs."""

import json
from unittest.mock import AsyncMock

import httpx

from app.infrastructure.workbench import capability_mcp as mcp


def transport_remote(monkeypatch, url, outcome):
    calls = []

    def handler(request):
        message = json.loads(request.content)
        if message["method"] == "initialize":
            if outcome == "handshake_timeout":
                raise httpx.ReadTimeout("handshake", request=request)
            return httpx.Response(
                200,
                json={
                    "id": message["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
            )
        if message["method"].startswith("notifications/"):
            return httpx.Response(202)
        assert message["method"] == "tools/call"
        calls.append(message)
        errors = {
            "connect_timeout": httpx.ConnectTimeout,
            "read_timeout": httpx.ReadTimeout,
            "connection_lost": httpx.ReadError,
            "write_error": httpx.WriteError,
        }
        if outcome in errors:
            raise errors[outcome]("transport failure", request=request)
        if outcome == "http_error":
            return httpx.Response(502)
        if outcome == "oversize":
            return httpx.Response(200, content=b"x" * (mcp.MAX_BYTES + 1))
        if outcome == "malformed":
            return httpx.Response(200, content=b"not json")
        if outcome == "wrong_id":
            return httpx.Response(200, json={"id": 999, "result": {}})
        if outcome == "protocol_error":
            return httpx.Response(
                200,
                json={
                    "id": message["id"],
                    "error": {"code": -32602, "message": "Invalid arguments"},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": message["id"],
                "result": {
                    "content": [{"type": "text", "text": "explicit result"}],
                    "isError": outcome == "tool_error",
                },
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        mcp,
        "public_address",
        AsyncMock(return_value=("93.184.216.34", "tools.example.test")),
    )
    monkeypatch.setattr(
        mcp.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    return mcp.RemoteMcp((url,)), calls
