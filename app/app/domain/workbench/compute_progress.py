"""Stop a broken computation call loop without changing financial methods."""

from dataclasses import dataclass, field
from typing import Any

MISSING_TASK_LIMIT = 3
MISSING_TASK_MESSAGE = (
    "计算工具连续3次缺少委托编号，计算尚未完成，已停止本轮。"
    "请核对工具调用后再决定是否重做；不会自动重试或追加费用。"
)


@dataclass
class ComputeProgressGuard:
    seen: set[str] = field(default_factory=set)
    missing: int = 0
    failure: str | None = None

    def observe(self, item: dict[str, Any]) -> None:
        if (
            self.failure
            or item.get("type") != "mcpToolCall"
            or item.get("server") != "sunmoon_workbench"
            or item.get("tool") != "compute"
        ):
            return
        identity = item.get("id")
        if not isinstance(identity, str) or identity in self.seen:
            return
        self.seen.add(identity)
        result = item.get("result") or {}
        failed = (
            item.get("status") == "failed"
            or bool(item.get("error"))
            or (isinstance(result, dict) and result.get("isError") is True)
        )
        args = item.get("arguments")
        task = args.get("task") if isinstance(args, dict) else None
        if not failed or (isinstance(task, str) and task.strip()):
            self.missing = 0
            return
        self.missing += 1
        if self.missing >= MISSING_TASK_LIMIT:
            self.failure = "missing_compute_task"
