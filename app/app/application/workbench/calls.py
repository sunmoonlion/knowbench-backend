"""Common call evidence; permissions still come from the verified host and grants."""

from __future__ import annotations

import uuid
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.tool_invocation import ToolInvocation
from app.domain.workbench.capabilities import digest
from app.domain.workbench.errors import RecordsRefused
from app.domain.workbench.states import TASK_TERMINAL, AttemptState
from app.domain.workbench.tool_registry import ToolRegistration


async def call_context(
    repo: WorkbenchStore, owner: str, invocation: ToolInvocation, *, task_id: str | None
) -> dict:
    """Link an existing host Task, never a model-supplied parent or a new retry Task.

    Requires a verified invocation and an enclosing transaction. This parent is an
    audit identity, NOT a permission grant or a claim that nested methods exist.
    """
    session = await repo.get_session(invocation.session_id, owner_actor_id=owner)
    attempt = await repo.attempt_for_runtime_turn(
        session_id=invocation.session_id, turn_id=invocation.turn_id
    )
    active = session.get("active_task_id")
    if (str(active) if active else None) != task_id:
        raise RecordsRefused("the execution context changed after authorization")
    if attempt and str(attempt["task_id"]) != str(task_id):
        raise RecordsRefused("the call's execution is no longer active")
    if not task_id:
        return {"parent_call_id": None, "task_id": None, "attempt_id": None}
    task = await repo.get_task(str(task_id), owner_actor_id=owner, for_update=True)
    if (
        task["state"] in TASK_TERMINAL
        or str(task["session_id"]) != invocation.session_id
    ):
        raise RecordsRefused("the call's task is no longer active")
    if attempt and (
        str(attempt["id"]) != str(task.get("active_attempt_id"))
        or attempt["status"] not in (AttemptState.CREATED, AttemptState.RUNNING)
    ):
        raise RecordsRefused("the call's execution attempt is no longer active")
    # Locking the Task serializes first creation with Task terminal transitions.
    key = "task:" + str(task_id)
    root = await repo.call_by_key(owner, invocation.session_id, key)
    if root is None:
        root = await repo.add_call(
            {
                "id": str(uuid.uuid4()),
                "owner_actor_id": owner,
                "session_id": invocation.session_id,
                "kind": "method",
                "origin": "method",
                "call_key": key,
                "task_id": str(task_id),
                "digest": digest([key]),
                "status": task["state"].lower(),
                "detail": {
                    "task_id": str(task_id),
                    "profile_id": task.get("profile_id"),
                    "profile_version": task.get("profile_version"),
                    "expert_pack_version": task.get("expert_pack_version"),
                    "identity_basis": "host_task",
                },
            }
        )
    return {
        "parent_call_id": root["id"],
        "task_id": str(task_id),
        "attempt_id": str(attempt["id"]) if attempt else None,
    }


class CallLedger:
    def __init__(self, repo: WorkbenchStore, owner: str):
        self.repo, self.owner = repo, owner

    async def begin(
        self,
        invocation: ToolInvocation,
        registration: ToolRegistration,
        arguments: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> tuple[dict, bool]:
        intent = {
            "invocation": invocation.snapshot(),
            "tool": registration.descriptor(),
            "arguments": arguments,
        }
        identity = digest(intent)
        await self.repo.recover_calls(self.owner, invocation.session_id)
        async with self.repo.transaction():
            context = await call_context(
                self.repo, self.owner, invocation, task_id=task_id
            )
            call_id = str(uuid.uuid4())
            call = await self.repo.add_call(
                {
                    **context,
                    "id": call_id,
                    "owner_actor_id": self.owner,
                    "session_id": invocation.session_id,
                    "kind": "tool",
                    "origin": "platform",
                    "call_key": invocation.key,
                    "registration_id": registration.tool_id,
                    "registration_digest": registration.descriptor_digest,
                    "digest": identity,
                    "status": "dispatching",
                    "detail": intent,
                }
            )
            if call["digest"] != identity or call["origin"] != "platform":
                raise RecordsRefused(
                    "invocation identity was reused for a different operation"
                )
            return call, call["id"] == call_id

    async def finish(self, call: dict, response: dict, status: str) -> None:
        # The MCP request id is transport-local and must not be replayed from storage.
        result = {k: v for k, v in response.items() if k not in ("id", "jsonrpc")}
        async with self.repo.transaction():
            await self.repo.update_call(
                self.owner, call["id"], status, {**call["detail"], "response": result}
            )
