"""状态词与合法转换。真源：tree-build/SDD/architecture/state-machine.md。

任何入口（网页接口、编排、超时扫描器、管理后台）都必须经 validate_task_transition / validate_attempt_transition，
不得各自判断。状态词不增：workflow 的步骤只动游标（Task.current_step），方向盘是 Session 属性。
"""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TASK_TERMINAL: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.REJECTED, TaskState.FAILED, TaskState.CANCELLED}
)

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.RECEIVED: frozenset({TaskState.VALIDATING, TaskState.CANCELLED}),
    TaskState.VALIDATING: frozenset(
        {TaskState.QUEUED, TaskState.WAITING, TaskState.REJECTED, TaskState.CANCELLED}
    ),
    TaskState.QUEUED: frozenset(
        {TaskState.RUNNING, TaskState.WAITING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.QUEUED,
            TaskState.WAITING,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING: frozenset(
        {TaskState.VALIDATING, TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.REJECTED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class WaitingReason(StrEnum):
    INPUT = "INPUT"
    APPROVAL = "APPROVAL"
    RESOURCE = "RESOURCE"
    ENVIRONMENT = "ENVIRONMENT"


class AttemptState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"


# 取消或收尾时 Codex 里那一轮可能还在跑：项目先不放行下一位，等那一轮确认结束，最多等这么久
# （同专家每一步的时限；SDD 0034 §三.1）。过了还没确认，放行并记「费用待核对」
TURN_HOLD_SECONDS = 900

# 平台自己出错（驾驶中抛了没接住的异常）：这次尝试与委托都记失败，测试结果里记出错而不是没过
PLATFORM_FAILURE = "platform"


ATTEMPT_TERMINAL: frozenset[AttemptState] = frozenset(
    {
        AttemptState.COMPLETED,
        AttemptState.FAILED,
        AttemptState.BUDGET_EXCEEDED,
        AttemptState.CANCELLED,
        AttemptState.ABANDONED,
    }
)

ATTEMPT_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.CREATED: frozenset(
        {AttemptState.RUNNING, AttemptState.CANCELLED, AttemptState.FAILED}
    ),
    AttemptState.RUNNING: frozenset(
        {
            AttemptState.SUSPENDED,
            AttemptState.COMPLETED,
            AttemptState.FAILED,
            AttemptState.BUDGET_EXCEEDED,
            AttemptState.CANCELLED,
        }
    ),
    AttemptState.SUSPENDED: frozenset(
        {AttemptState.RUNNING, AttemptState.ABANDONED, AttemptState.CANCELLED}
    ),
    AttemptState.COMPLETED: frozenset(),
    AttemptState.FAILED: frozenset(),
    AttemptState.BUDGET_EXCEEDED: frozenset(),
    AttemptState.CANCELLED: frozenset(),
    AttemptState.ABANDONED: frozenset(),
}


class AttemptRole(StrEnum):
    execute = "execute"
    acceptance = "acceptance"
    competitor = "competitor"  # 第一期不开（C-A10），位置留好
    judge = "judge"


class Wheel(StrEnum):
    user = "user"
    advisor = "advisor"


class InvalidTransition(ValueError):
    pass


def validate_task_transition(
    current: str, target: str, *, waiting_reason: str | None = None
) -> None:
    cur, tgt = TaskState(current), TaskState(target)
    if tgt not in TASK_TRANSITIONS[cur]:
        raise InvalidTransition(f"invalid task transition {cur}->{tgt}")
    if tgt is TaskState.WAITING:
        if waiting_reason is None:
            raise InvalidTransition("WAITING requires a reason")
        WaitingReason(waiting_reason)


def validate_attempt_transition(current: str, target: str) -> None:
    cur, tgt = AttemptState(current), AttemptState(target)
    if tgt not in ATTEMPT_TRANSITIONS[cur]:
        raise InvalidTransition(f"invalid attempt transition {cur}->{tgt}")


def wheel_after_task_state(state: str) -> Wheel | None:
    """Task 进任一终态时方向盘交回用户（I12）；其它状态不动。"""
    return Wheel.user if TaskState(state) in TASK_TERMINAL else None
