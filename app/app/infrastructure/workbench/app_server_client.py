"""对沙箱里 `codex app-server` 的 WebSocket JSON-RPC 客户端（通道②）。

只经公开协议驱动（C-C7）：request/response、通知、服务端请求（审批）。握手带 Authorization: Bearer <能力令牌>。
断线不在这里重连：把 closed 事件交给上层，由 runner 决定何时重连（Attempt 的 SUSPENDED 语义在账房）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Callable, Mapping
from ipaddress import ip_address
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from app.application.ports.workbench import (
    AppServerError,
    NotificationHandler,
    ServerRequestHandler,
)

__all__ = [
    "AppServerClient",
    "AppServerError",
    "WsAppServerConnector",
    "resolve_token_ref",
]

log = logging.getLogger(__name__)


def resolve_token_ref(ref: str) -> str:
    """token_ref 只存引用不存令牌：env:NAME | file:/abs/path | inline:<仅测试与本机联调>。"""
    scheme, _, rest = ref.partition(":")
    if scheme == "env":
        value = os.environ.get(rest)
        if not value:
            raise RuntimeError(f"app-server token env {rest} is empty")
        return value.strip()
    if scheme == "file":
        with open(rest) as f:
            return f.read().strip()
    if scheme == "inline":
        return rest
    raise RuntimeError(f"unsupported token_ref scheme: {scheme}")


class AppServerClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        on_notification: NotificationHandler,
        on_server_request: ServerRequestHandler,
        client_name: str = "sunmoon-workbench",
        client_version: str = "0.1.0",
    ):
        self.url = url
        self._token = token
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._client_name = client_name
        self._client_version = client_version
        self._ws: ClientConnection | None = None
        self._pending: dict[
            int, tuple[str, asyncio.Future, Callable[[Any], None] | None]
        ] = {}
        self._next_id = 1
        self._reader: asyncio.Task | None = None
        self._handlers: set[asyncio.Task] = set()
        self.closed = asyncio.Event()
        self.closed.set()
        self.server_info: dict[str, Any] = {}

    @classmethod
    def instruction_worker(cls, address, token, **handlers):
        ip = ip_address(address)
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        client = cls(f"ws://{host}:47800", token, **handlers)
        client._worker_address = str(ip)
        return client

    _worker_address: str | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self.closed.is_set()

    async def connect(self) -> None:
        sock = None
        try:
            if self._worker_address is not None:
                # A connected socket pins this credential to the supplied Pod and prevents
                # environment proxies or WebSocket redirects from receiving its bearer.
                family = (
                    socket.AF_INET6 if ":" in self._worker_address else socket.AF_INET
                )
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setblocking(False)
                await asyncio.wait_for(
                    asyncio.get_running_loop().sock_connect(
                        sock, (self._worker_address, 47800)
                    ),
                    10,
                )
            self._ws = await connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self._token}"},
                max_size=None if sock is None else 2 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
                sock=sock,
            )
        except BaseException:
            if sock is not None:
                sock.close()
            raise
        self.closed.clear()
        self._reader = asyncio.create_task(
            self._read_loop(), name=f"app-server-reader:{self.url}"
        )
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self._client_name,
                    "title": self._client_name,
                    "version": self._client_version,
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.server_info = result or {}
        await self.notify("initialized", {})

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader is not None:
            if not self.closed.is_set():
                self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        self._finish_closed()

    def _finish_closed(self) -> None:
        self.closed.set()
        for _, fut, _ in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("app-server connection closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("app-server sent non-JSON frame")
                    continue
                if "id" in msg and "method" in msg:
                    handler = asyncio.create_task(
                        self._safe_server_request(
                            msg["id"], msg["method"], msg.get("params") or {}
                        )
                    )
                    self._handlers.add(handler)
                    handler.add_done_callback(self._handlers.discard)
                elif "id" in msg:
                    pending = self._pending.pop(msg["id"], None)
                    if pending is None:
                        continue
                    method, fut, on_result = pending
                    if not fut.done():
                        if "error" in msg:
                            error = msg["error"]
                            if not isinstance(error, dict):
                                error = {
                                    "message": "Malformed app-server error response"
                                }
                            fut.set_exception(AppServerError(method, error))
                        else:
                            result = msg.get("result")
                            try:
                                if on_result is not None:
                                    on_result(result)
                            except Exception as exc:
                                fut.set_exception(exc)
                            else:
                                fut.set_result(result)
                elif "method" in msg:
                    try:
                        await self._on_notification(
                            msg["method"], msg.get("params") or {}
                        )
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "notification handler failed method=%s", msg["method"]
                        )
        except websockets.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001
            log.exception("app-server reader crashed")
        finally:
            self._finish_closed()
            handlers = tuple(self._handlers)
            for handler in handlers:
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    async def _safe_server_request(
        self, rid: Any, method: str, params: dict[str, Any]
    ) -> None:
        try:
            await self._on_server_request(rid, method, params)
        except Exception as exc:  # noqa: BLE001
            log.exception("server request handler failed method=%s", method)
            try:
                await self.respond_error(rid, -32603, f"handler failed: {exc}")
            except websockets.ConnectionClosed:
                pass

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60,  # noqa: ASYNC109
        on_result: Callable[[Any], None] | None = None,
    ) -> Any:
        if self._ws is None or self.closed.is_set():
            raise ConnectionError("app-server not connected")
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = (method, fut, on_result)
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "method": method,
                        "params": params or {},
                    }
                )
            )
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._ws is None:
            raise ConnectionError("app-server not connected")
        await self._ws.send(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        )

    async def respond(self, rid: Any, result: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))

    async def respond_error(self, rid: Any, code: int, message: str) -> None:
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": code, "message": message},
                }
            )
        )


class WsAppServerConnector:
    """端口 `AppServerConnector` 的实现：解出令牌，建一条 WebSocket 连接。"""

    def open(
        self,
        sandbox: Mapping[str, Any],
        *,
        on_notification: NotificationHandler,
        on_server_request: ServerRequestHandler,
    ) -> AppServerClient:
        if not (
            sandbox.get("provisioned") is True
            or sandbox.get("operator_registered") is True
        ):
            raise RuntimeError(
                "sandbox requires platform provisioning or operator registration"
            )
        return AppServerClient(
            sandbox["app_server_url"],
            resolve_token_ref(sandbox["token_ref"]),
            on_notification=on_notification,
            on_server_request=on_server_request,
        )
