"""Bounded Streamable HTTP MCP adapter with pinned public DNS and no redirects."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from app.domain.workbench.capabilities import CapabilityError, CapabilityReauthRequired

MAX_BYTES = 1024 * 1024


async def public_address(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise CapabilityError("Invalid MCP endpoint") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or port not in (None, 443)
    ):
        raise CapabilityError(
            "Only reviewed HTTPS endpoints on port 443 without credentials or query strings are supported"
        )
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parts.hostname, 443, type=socket.SOCK_STREAM
            ),
            5,
        )
    except (TimeoutError, OSError) as exc:
        raise CapabilityError("MCP endpoint DNS is unavailable") from exc
    ips = [str(a[4][0]) for a in addresses]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise CapabilityError(
            "MCP endpoint must resolve exclusively to public addresses"
        )
    # The URL used for the socket is this exact IP; Host and TLS SNI keep the original identity.
    return ips[0], parts.hostname


class RemoteMcp:
    def __init__(self, allowed_urls: tuple[str, ...] = ()):
        self.allowed_urls = allowed_urls

    @asynccontextmanager
    async def connect(self, url, token):
        if url not in self.allowed_urls:
            raise CapabilityError(
                "This MCP endpoint is not in the reviewed service directory"
            )
        ip, host = await public_address(url)
        target = httpx.URL(url).copy_with(host=ip)
        headers = {
            "Host": host,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            sequence = 0

            async def rpc(method, params=None, *, notification=False):
                nonlocal sequence
                sequence += 1
                body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
                if not notification:
                    body["id"] = sequence
                try:
                    async with client.stream(
                        "POST",
                        target,
                        headers=headers,
                        json=body,
                        extensions={"sni_hostname": host},
                    ) as response:
                        if response.status_code in (401, 403):
                            raise CapabilityReauthRequired("MCP authentication requires renewal")
                        if response.status_code not in (200, 202, 204):
                            raise CapabilityError("MCP service rejected the request")
                        session_id = response.headers.get("mcp-session-id")
                        if session_id and len(session_id) <= 256:
                            headers["Mcp-Session-Id"] = session_id
                        if notification:
                            return {}
                        data = bytearray()
                        reply = None
                        sse = "text/event-stream" in response.headers.get(
                            "content-type", ""
                        )
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_BYTES:
                                raise CapabilityError(
                                    "MCP response exceeds the size limit"
                                )
                            if sse:
                                # Return at the matching complete event, not at stream EOF.
                                events = (
                                    bytes(data)
                                    .replace(b"\r\n", b"\n")
                                    .split(b"\n\n")[:-1]
                                )
                                for event in events:
                                    value = b"\n".join(
                                        line[5:].lstrip()
                                        for line in event.splitlines()
                                        if line.startswith(b"data:")
                                    )
                                    if value:
                                        candidate = json.loads(value)
                                        if (
                                            isinstance(candidate, dict)
                                            and candidate.get("id") == sequence
                                        ):
                                            reply = candidate
                                            break
                                if reply is not None:
                                    break
                        if not sse:
                            reply = json.loads(data)
                        if (
                            not isinstance(reply, dict)
                            or reply.get("id") != sequence
                            or "error" in reply
                            or not isinstance(reply.get("result"), dict)
                        ):
                            raise CapabilityError(
                                "MCP returned an invalid or unsuccessful response"
                            )
                        return reply["result"]
                except (httpx.HTTPError, ValueError, UnicodeError, RecursionError) as exc:
                    raise CapabilityError(
                        "MCP response unavailable; a submitted operation may have an unknown outcome"
                    ) from exc

            hello = await rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "sunmoon-capabilities", "version": "1"},
                },
            )
            version = hello.get("protocolVersion")
            if version not in ("2025-06-18", "2025-03-26"):
                raise CapabilityError("Unsupported MCP protocol version")
            headers["MCP-Protocol-Version"] = version
            await rpc("notifications/initialized", notification=True)
            yield rpc

    async def discover(self, url, token):
        try:
            return await self._discover(url, token)
        except TimeoutError as exc:
            raise CapabilityError("MCP discovery timed out") from exc

    async def _discover(self, url, token):
        async with asyncio.timeout(45), self.connect(url, token) as rpc:
            tools, cursor, seen = [], None, set()
            for _ in range(10):
                page = await rpc("tools/list", {"cursor": cursor} if cursor else {})
                if not isinstance(page.get("tools"), list):
                    raise CapabilityError("MCP tool list is missing")
                for tool in page["tools"]:
                    if (
                        not isinstance(tool, dict)
                        or not isinstance(tool.get("name"), str)
                        or not tool["name"]
                        or len(tool["name"]) > 128
                        or tool["name"] in seen
                        or not isinstance(tool.get("inputSchema"), dict)
                    ):
                        raise CapabilityError(
                            "Invalid or duplicate MCP tool description"
                        )
                    seen.add(tool["name"])
                    tools.append(
                        {
                            "name": tool["name"],
                            "description": str(tool.get("description", ""))[:4000],
                            "inputSchema": tool["inputSchema"],
                        }
                    )
                    if len(tools) > 100:
                        raise CapabilityError("MCP has more than 100 tools")
                cursor = page.get("nextCursor")
                if not cursor:
                    return tools
            raise CapabilityError("MCP pagination exceeds the limit")

    async def call(self, url, token, name, arguments):
        # Never retry tools/call: transport failure after dispatch is an unknown outcome.
        async with asyncio.timeout(45), self.connect(url, token) as rpc:
            return await rpc("tools/call", {"name": name, "arguments": arguments})
