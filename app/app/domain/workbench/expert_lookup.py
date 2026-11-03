"""Small, bounded directory lookup; limits never apply to author methods."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

LOOKUP_SECONDS = 120
STOP_REASONS = {
    "directory_unavailable": "专家目录读取失败，已停止查找；这不代表没有合适专家",
    "tool_approval_required": "平台的目录工具权限配置冲突，已停止查找；这不代表没有合适专家，请等待平台修复",
    "repeated_lookup": "重复读取专家目录仍未交回选择，已停止查找",
    "unexpected_tool": "查找偏离专家目录，已停止额外操作",
    "time_limit": "查找超过2分钟，已停止；未自动重新查找",
    "budget_limit": "查找已达到本轮预算，已停止；未自动追加预算",
}


@dataclass
class LookupGuard:
    remaining: Decimal | None
    failure: str | None = None
    calls: int = 0
    seen: set[str] = field(default_factory=set)

    def observe(self, item: dict[str, Any]) -> None:
        if self.failure:
            return
        kind = item.get("type")
        if kind in ("commandExecution", "webSearch", "fileChange"):
            self.failure = "unexpected_tool"
        if kind != "mcpToolCall" or item.get("tool") != "find_expert":
            return
        result = item.get("result") or {}
        if (
            item.get("status") == "failed"
            or item.get("error")
            or (isinstance(result, dict) and result.get("isError"))
        ):
            error = item.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            self.failure = (
                "tool_approval_required"
                if message
                == "MCP tool call requires approval, but approval policy is never"
                else "directory_unavailable"
            )
            return
        identity = item.get("id")
        if not isinstance(identity, str) or identity in self.seen:
            return
        self.seen.add(identity)
        self.calls += 1
        if self.calls >= 3:
            self.failure = "repeated_lookup"

    def usage(self, cost: Any) -> None:
        if self.failure or self.remaining is None or cost is None:
            return
        try:
            used = Decimal(str(cost))
        except ArithmeticError:
            return
        if used.is_finite() and used >= self.remaining:
            self.failure = "budget_limit"
