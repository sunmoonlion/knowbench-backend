"""回答一个「待人回答」的交互：消费它，再把该排的命令排上（网页与跑分身测试共用）。

委托回到排队的，排一条 task.drive 接着驾驶；挂在 app-server 那边的请求（审批、Codex 反问，
带 request_id）还在等，排一条 approval.respond 把答复送到正握着连接的 runner。开步前的提问
是委托级的，没有 request_id，只需接着驾驶。
"""

from __future__ import annotations

from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.ledger import Ledger


async def answer_interaction(
    repo: WorkbenchStore,
    interaction_id: str,
    *,
    token: str,
    response: dict[str, Any],
    owner_actor_id: str,
    subject_digest: str | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    async with repo.transaction():
        it = await repo.get_interaction(interaction_id)
        result = await Ledger(repo).respond_interaction(
            interaction_id,
            token=token,
            response=response,
            owner_actor_id=owner_actor_id,
            subject_digest=subject_digest,
        )
        if it["task_id"] is not None and result.get("state") == "QUEUED":
            session = await repo.get_session(str(it["session_id"]))
            await repo.enqueue_command(
                session_id=str(it["session_id"]),
                sandbox_id=str(session["sandbox_id"]),
                kind="task.drive",
                payload={"task_id": str(it["task_id"])},
            )
        request_id = ((it.get("prompt") or {}).get("subject") or {}).get("request_id")
        if it["kind"] in ("tool_approval", "question") and request_id:
            session = await repo.get_session(str(it["session_id"]))
            await repo.enqueue_command(
                session_id=str(it["session_id"]),
                sandbox_id=str(session["sandbox_id"]),
                kind="approval.respond",
                payload={
                    "request_id": request_id,
                    "decision": decision
                    or ("answer" if it["kind"] == "question" else "decline"),
                    "response": response,
                },
            )
    return result


async def cancel_task(
    repo: WorkbenchStore, task_id: str, *, owner_actor_id: str
) -> dict[str, Any]:
    """停下一个委托：先记下取消意图；专家正在做某一步（在跑或卡在等人）就马上中断这一轮。"""
    result = await Ledger(repo).request_cancel(task_id, owner_actor_id=owner_actor_id)
    if result.get("interrupt"):
        task = await repo.get_task(task_id)
        talk = await repo.get_session(str(task["session_id"]))
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=str(task["session_id"]),
                sandbox_id=str(talk["sandbox_id"]),
                kind="turn.interrupt",
                payload={"by": "user", "task_id": task_id},
            )
    return result
