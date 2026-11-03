"""The single sandbox turn boundary shared by method planning and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

STEP_TIMEOUT_SECONDS = 900


@dataclass
class TurnResult:
    turn_id: str | None
    final_text: str | None
    tokens: dict[str, Any]  # 这一轮各次调用相加的用量（pricing.Usage.as_json）
    error: str | None = None
    platform_failure: bool = False
    environment_lost: bool = False
    # 这一轮花了多少（估算）。不认识这个模型的单价时是 None：只有用量，没有金额
    cost: Decimal | None = None
    interrupted: bool = False
    # 用户在审批面点了「停下」（或等不到人）：这一轮是被人停的，不是出错
    stopped_by_user: bool = False
    stop_note: str | None = None
    # 这一轮用补丁改到的文件（绝对路径）；成稿文件写了没有就看它（0015 §3.3）
    files: list[str] = field(default_factory=list)
    # 这一步到了时限还没做完，被工作台停下：最后一句只是进度说明，不是交回物，不拿去验
    timed_out: bool = False
    # Confirmed interruption by the platform data-planning progress guard.
    planning_failure: str | None = None
    planning_stop_unconfirmed: bool = False
    lookup_failure: str | None = None
    lookup_stop_unconfirmed: bool = False
    tool_stop_unconfirmed: bool = False


class TurnDriver(Protocol):
    """runner 的 SandboxLink 实现它：在给定 thread 上发一个 turn 并等它结束。"""

    async def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        timeout: float = STEP_TIMEOUT_SECONDS,  # noqa: ASYNC109
    ) -> TurnResult: ...
