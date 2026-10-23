"""会合点 `/service` 的 WebSocket 实现（0022）：工作台这头一问一答。规矩与错误映射在 application.workbench.agent_service。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.application.workbench.agent_service import (
    CALL_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    REJECT_REASONS,
    SERVICE_PROTOCOL,
    SERVICE_SOFTWARE,
    AgentRefused,
    AgentServiceFailed,
)
from app.application.workbench.tokens import TokenIssuer
from app.domain.workbench.errors import WorkbenchError


class WsAgentService:
    """一问一答：每次调用开一条到会合点 `/service` 的流，问完就关。先这么做；要省连接再做复用。"""

    def __init__(
        self,
        relay_url: str,
        issuer: TokenIssuer,
        *,
        timeout: float = CALL_TIMEOUT_SECONDS,
    ) -> None:
        self.url = relay_url.rstrip("/") + "/service"
        self.issuer = issuer
        self.timeout = timeout

    async def call(
        self,
        relay_user: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        from websockets.asyncio.client import connect

        token = self.issuer.workbench_token(relay_user).token
        hello = {
            "type": "hello",
            "proto": SERVICE_PROTOCOL,
            "role": "workbench",
            "user": relay_user,
            "token": token,
            "software": SERVICE_SOFTWARE,
        }
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        try:
            async with connect(
                self.url, open_timeout=10, max_size=MAX_FRAME_BYTES + 4096
            ) as ws:
                await ws.send(json.dumps(hello))
                first = _as_dict(await asyncio.wait_for(ws.recv(), 15))
                if first.get("type") == "reject":
                    reason = str(first.get("reason") or "")
                    exc_type = REJECT_REASONS.get(reason, AgentServiceFailed)
                    raise exc_type(f"relay refused: {reason or 'unknown'}")
                if first.get("type") != "welcome":
                    raise AgentServiceFailed("relay did not welcome the service stream")
                await ws.send(json.dumps(request))
                deadline = time.monotonic() + (
                    self.timeout if wait_seconds is None else wait_seconds
                )
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AgentServiceFailed("agent did not answer in time")
                    frame = _as_dict(await asyncio.wait_for(ws.recv(), remaining))
                    if frame.get("id") == 1:
                        break
        except WorkbenchError:
            raise
        except (OSError, ValueError, TimeoutError) as exc:
            raise AgentServiceFailed("agent service stream unreachable") from exc
        except Exception as exc:  # websockets 的异常族
            raise AgentServiceFailed("agent service stream failed") from exc
        error = frame.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            raise AgentRefused(
                code if isinstance(code, int) else -32000,
                str(error.get("message") or "agent refused"),
            )
        result = frame.get("result")
        if not isinstance(result, dict):
            raise AgentServiceFailed("agent answered without a result")
        return result


def _as_dict(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raise AgentServiceFailed("binary frame on the service stream")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AgentServiceFailed("service frame is not an object")
    return value
