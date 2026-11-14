"""Resume revoked methods only after their previous turn has stopped and settled."""

from app.application.workbench.ledger import Ledger
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    AttemptState,
    TaskState,
    WaitingReason,
)


async def hold_revoked_method(repo, task, attempt, *, cost_unknown=False):
    """Caller holds the task lock and has settled the stopped attempt's usage."""
    task_id = str(task["id"])
    led = Ledger(repo)
    if task["state"] == TaskState.WAITING:
        # An external MCP approval is owned by the same turn, but is not a native
        # app-server request. Invalidate its token before replacing the wait.
        await repo.cancel_pending_interactions(
            task_id=task_id, reason="method permission revoked"
        )
        await led.transition(
            task_id,
            TaskState.QUEUED,
            active_interaction_id=None,
            reason={"method_permission_revoked": True},
        )
        await led.transition(task_id, TaskState.RUNNING)
    revocation = attempt["refs"]["permission_revocation"]
    await led.open_interaction(
        task_id,
        kind="question",
        waiting_reason=WaitingReason.INPUT,
        prompt=InteractionPrompt(
            title="方法权限已撤销，本步骤已暂停",
            question=(
                (
                    "本轮已停止，本轮费用待核对，已有调用结果已保留。"
                    if cost_unknown
                    else "本轮已停止，已产生的费用和调用结果已保留。"
                )
                + "请在「本次能力」重新确认所需权限，再继续本步骤；"
                "继续前请核对已完成或结果未知的外部操作。"
            ),
            options=[{"value": "continue", "label": "已确认权限，继续"}],
            subject={
                "capability_binding": revocation["context"],
                "step_id": attempt["step_id"],
                "method_permission_revoked": True,
            },
        ),
    )
    return TaskState.WAITING


async def recover_revoked_method(repo, snapshot, *, stopped):
    """A restarted runner may offer reconfirmation only after a verified stop."""
    if not stopped:
        return None
    async with repo.transaction():
        task = await repo.get_task(str(snapshot["id"]), for_update=True)
        if (
            task["state_version"] != snapshot["state_version"]
            or task["state"] not in (TaskState.RUNNING, TaskState.WAITING)
            or task.get("cancel_requested_at")
            or not task.get("active_attempt_id")
        ):
            return None
        attempt = await repo.get_attempt(
            str(task["active_attempt_id"]), for_update=True
        )
        if attempt["status"] in ATTEMPT_TERMINAL or not (attempt.get("refs") or {}).get(
            "permission_revocation"
        ):
            return None
        led = Ledger(repo)
        if attempt["status"] == AttemptState.SUSPENDED:
            await led.attempt_transition(str(attempt["id"]), AttemptState.RUNNING)
        await led.attempt_transition(
            str(attempt["id"]),
            AttemptState.FAILED,
            failure_code="method_permission_revoked",
            retryable=True,
            turn_id=(attempt.get("refs") or {}).get("runtime_turn_id"),
            usage_pending=True,
        )
        await repo.append_event(
            session_id=str(task["session_id"]),
            task_id=str(task["id"]),
            attempt_id=str(attempt["id"]),
            kind="task",
            event_type="method/permissionStopRecovered",
            payload={"cost": "unknown", "stopped": True},
        )
        return await hold_revoked_method(repo, task, attempt, cost_unknown=True)
