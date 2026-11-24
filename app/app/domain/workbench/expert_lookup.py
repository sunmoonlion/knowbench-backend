"""Small, bounded directory lookup; limits never apply to author methods."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.workbench.errors import WorkbenchError


class LegacyMatchRetired(WorkbenchError):
    code = "legacy_expert_match_retired"
    http_status = 410

    def __init__(self):
        super().__init__("旧版专家匹配已退役。请从「请专家」开始新匹配。")


def legacy_match(profile, version):
    return profile == "FIND_EXPERT" and str(version or "1") in {"1", "2", "3", "4"}


LOOKUP_SECONDS: float | None = None
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

    def observe(self, item: dict[str, Any]) -> None:
        if self.failure:
            return
        kind = item.get("type")
        if kind in ("commandExecution", "webSearch", "fileChange", "mcpToolCall"):
            self.failure = "unexpected_tool"

    def usage(self, cost: Any) -> None:
        if self.failure or self.remaining is None or cost is None:
            return
        try:
            used = Decimal(str(cost))
        except ArithmeticError:
            return
        if used.is_finite() and used >= self.remaining:
            self.failure = "budget_limit"
