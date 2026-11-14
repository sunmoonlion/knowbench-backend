"""Common call evidence; permissions still come from the verified host and grants."""

from __future__ import annotations

import uuid
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.call_policy import authorize_operation
from app.application.workbench.method_calls import root_call
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
    root = await root_call(repo, task)
    parent = root
    refs = (attempt or {}).get("refs") or {}
    if refs.get("kind") == "method_step":
        # Recompute the expected identity from the accepted plan rather than
        # trusting an arbitrary parent ID (even one attached to an Attempt).
        from app.application.workbench.execution_plan import (
            _meta,
            active_method_step,
            execution_for_task,
        )
        from app.application.workbench.method_calls import step_call

        view = await execution_for_task(repo, task)
        if view is None:
            raise RecordsRefused("method plan is unavailable")
        # An exact-action approval can temporarily put this same Attempt in WAITING.
        await active_method_step(repo, {**task, "state": "RUNNING"}, view)
        parent = await step_call(repo, task, _meta(view, int(task["current_step"])))
        if refs.get("method_call_id") != parent["id"]:
            raise RecordsRefused("method invocation does not match its host binding")
    return {
        "parent_call_id": parent["id"],
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
        decision = authorize_operation(registration, arguments)
        if decision.requires_approval:
            raise RecordsRefused("this operation requires the approved adapter path")
        intent = {
            "invocation": invocation.snapshot(),
            "tool": registration.descriptor(),
            "arguments": arguments,
            "operation": decision.operation,
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
