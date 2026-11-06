"""Observable progress limits; activity alone is not evidence of useful work.

The caller supplies a monotonic clock with human waiting removed. Tool execution
has its own limit and does not consume the model's idle allowance. No financial
method, output schema, or fee calculation lives here.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# No wall/idle/tool duration cutoff. Repeated-result protection still applies.
STEP_IDLE_SECONDS: float | None = None
STEP_TOOL_SECONDS: float | None = None
MAX_IDENTICAL_RESULTS = 3
# 一步这么久没有可见进展时只提醒、不中断、不自动停（账 183，所有者 11-06「给个半小时」）。
# 每再过这么久再提醒一次；None 关掉提醒。
STALL_NOTICE_SECONDS: float | None = 1800.0
STOP_REASONS = {
    "no_progress": "超过180秒无可见进展时限，已中止本轮。",
    "repeated_result": "同一工具以相同参数取得相同结果或错误累计3次，已中止本轮。",
    "tool_time_limit": "工具执行超过5分钟，已中止本轮。",
    "time_limit": "这一步超过15分钟仍未完成，已中止本轮。",
}
TOOL_TYPES = frozenset(
    {"mcpToolCall", "commandExecution", "fileChange", "dynamicToolCall", "webSearch"}
)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


@dataclass
class StepProgressGuard:
    clock: float
    idle_seconds: float = 0.0
    seen: set[tuple[str, str]] = field(default_factory=set)
    active: dict[str, float] = field(default_factory=dict)
    results: dict[str, int] = field(default_factory=dict)
    failure: str | None = None
    last_activity: float | None = None

    def tick(self, now: float) -> None:
        elapsed = max(0.0, now - self.clock)
        self.clock = now
        if self.failure:
            return
        if self.active:
            if STEP_TOOL_SECONDS is not None and any(
                now - started >= STEP_TOOL_SECONDS for started in self.active.values()
            ):
                self.failure = "tool_time_limit"
        else:
            self.idle_seconds += elapsed
            if STEP_IDLE_SECONDS is not None and self.idle_seconds >= STEP_IDLE_SECONDS:
                self.failure = "no_progress"

    def stalled_seconds(self, now: float) -> float:
        """多久没有可见进展：模型静默的累计（拿到新结果清零），与跑得最久的那个工具两者取大。
        先 `tick` 再问。只用来提醒，不判失败。"""
        running = now - min(self.active.values()) if self.active else 0.0
        return max(self.idle_seconds, running)

    def observe(self, method: str, item: dict[str, Any], now: float) -> None:
        self.tick(now)
        identity = item.get("id")
        if (
            not isinstance(identity, str)
            or not identity
            or method not in ("item/started", "item/completed")
        ):
            return
        key = (method, identity)
        if key in self.seen or self.failure:
            return
        self.seen.add(key)
        self.last_activity = now
        kind = item.get("type")
        if kind not in TOOL_TYPES:
            # Reasoning and prose, including changing progress announcements,
            # must never extend the allowance by merely asserting progress.
            return
        if method == "item/started":
            if ("item/completed", identity) not in self.seen:
                self.active[identity] = now
            return
        self.active.pop(identity, None)
        result = item.get("result")
        failed = (
            item.get("status") in ("failed", "declined", "cancelled")
            or bool(item.get("error"))
            or (isinstance(result, dict) and result.get("isError") is True)
            or (kind == "commandExecution" and item.get("exitCode") != 0)
        )
        signature = fingerprint(
            {
                "type": kind,
                "server": item.get("server"),
                "tool": item.get("tool"),
                "arguments": item.get("arguments"),
                "command": item.get("command"),
                "result": result,
                "error": item.get("error"),
                "failed": failed,
                "output": item.get("aggregatedOutput"),
                "changes": item.get("changes"),
            }
        )
        count = self.results.get(signature, 0) + 1
        self.results[signature] = count
        if count >= MAX_IDENTICAL_RESULTS:
            self.failure = "repeated_result"
        elif (
            not failed
            and count == 1
            and (result is not None or item.get("status") == "completed")
        ):
            self.idle_seconds = 0.0
