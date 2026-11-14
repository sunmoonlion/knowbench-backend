"""Shared provenance checks for MCP requests observed by the authenticated runner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import RecordsRefused


@dataclass(frozen=True)
class ToolInvocation:
    session_id: str
    thread_id: str
    turn_id: str
    call_id: str
    server: str

    @property
    def key(self) -> str:
        # Same arguments in another host call are a different intent.
        return digest(
            [self.session_id, self.thread_id, self.turn_id, self.server, self.call_id]
        )

    def snapshot(self) -> dict[str, str]:
        return {
            "key": self.key,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "call_id": self.call_id,
            "server": self.server,
        }


async def verify_tool_invocation(
    repo: WorkbenchStore,
    *,
    owner: str,
    sandbox: str,
    server: str,
    name: str,
    arguments: dict[str, Any],
    meta: Any,
    session_id: str | None = None,
    observe_seconds: float = 2,
) -> ToolInvocation:
    """Metadata is a lookup hint, never authority without the runner's event."""
    if not isinstance(meta, dict) or not isinstance(
        meta.get("x-codex-turn-metadata"), dict
    ):
        raise RecordsRefused("this call requires verified execution metadata")
    turn = meta["x-codex-turn-metadata"]
    thread, turn_id, call_id = (
        turn.get("thread_id"),
        turn.get("turn_id"),
        meta.get("callId"),
    )
    if (
        not all(
            isinstance(v, str) and 0 < len(v) <= 200 for v in (thread, turn_id, call_id)
        )
        or meta.get("threadId") != thread
    ):
        raise RecordsRefused("invalid execution metadata")
    assert (
        isinstance(thread, str)
        and isinstance(turn_id, str)
        and isinstance(call_id, str)
    )
    session = await repo.get_session_by_thread(thread)
    if (
        not session
        or str(session["owner_actor_id"]) != owner
        or (session_id is not None and str(session["id"]) != session_id)
    ):
        raise RecordsRefused("execution context not found")
    host = await repo.get_sandbox(str(session["sandbox_id"]))
    if str(host["owner_actor_id"]) != owner or sandbox not in {
        str(host["id"]),
        host.get("relay_user"),
    }:
        raise RecordsRefused("execution context not found")
    invocation = ToolInvocation(str(session["id"]), thread, turn_id, call_id, server)
    deadline = asyncio.get_running_loop().time() + observe_seconds
    while True:
        observed = await repo.observed_mcp_call(
            session_id=invocation.session_id, turn_id=turn_id, call_id=call_id
        )
        if observed:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise RecordsRefused("tool call has not been verified by the runner")
        await asyncio.sleep(0.05)
    item = observed.get("item") or {}
    try:
        arguments_match = json.dumps(
            item.get("arguments"), sort_keys=True, ensure_ascii=False, allow_nan=False
        ) == json.dumps(arguments, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise RecordsRefused("tool arguments must be finite JSON") from exc
    if (
        observed.get("threadId") != thread
        or item.get("server") != server
        or item.get("tool") != name
        or not arguments_match
    ):
        raise RecordsRefused("tool call differs from the runner's observed call")
    # The host/session binding may have changed while waiting for its event.
    fresh = await repo.get_session(invocation.session_id, owner_actor_id=owner)
    if fresh.get("thread_id") != thread or str(fresh["sandbox_id"]) != str(host["id"]):
        raise RecordsRefused("execution context changed")
    return invocation
