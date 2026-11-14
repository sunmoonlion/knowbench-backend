"""Verify dynamic MCP calls against the authenticated app-server event stream.

Model arguments and MCP metadata alone are not authority. The accepted plan,
active Attempt, turn/start response and observed tool event must agree. No
global sandbox configuration, additional model client or parallel task ledger.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.execution_plan import (
    active_method_step,
    execution_for_task,
    is_dynamic,
)
from app.application.workbench.ledger import Ledger
from app.application.workbench.records import as_id
from app.application.workbench.tool_invocation import verify_tool_invocation
from app.domain.workbench.errors import RecordsRefused
from app.domain.workbench.states import AttemptState, TaskState, Wheel

SERVER = "sunmoon_workbench"
# Data planning may discover requirements, but cannot execute author methods.
DATA_PLANNING_TOOLS = frozenset(
    {"find_subject", "describe_items", "request_data", "discover_source_tools"}
)


async def capture_attempt(repo: WorkbenchStore, thread_id: str):
    """Capture the expected identity before sending turn/start, never from args."""
    session = await repo.get_session_by_thread(thread_id)
    if not session or not session.get("active_task_id"):
        return None
    task = await repo.get_task(str(session["active_task_id"]))
    if (
        session["wheel"] != Wheel.advisor
        or task["state"] != TaskState.RUNNING
        or task.get("cancel_requested_at")
        or not task.get("active_attempt_id")
    ):
        raise RecordsRefused("no active expert execution")
    return str(task["id"]), str(task["active_attempt_id"])


async def bind_turn(
    repo: WorkbenchStore, expected: tuple[str, str], thread_id: str, turn_id: str
) -> bool:
    """Bind only the turn/start response; leave turn_ids for atomic fee settlement."""
    task_id, attempt_id = expected
    async with repo.transaction():
        task = await repo.get_task(task_id, for_update=True)
        session = await repo.get_session(str(task["session_id"]))
        if (
            task["state"] != TaskState.RUNNING
            or task.get("cancel_requested_at")
            or str(task.get("active_attempt_id")) != attempt_id
            or session["wheel"] != Wheel.advisor
            or str(session.get("active_task_id")) != task_id
            or session.get("thread_id") != thread_id
        ):
            return False
        attempt = await repo.get_attempt(attempt_id, for_update=True)
        refs = dict(attempt.get("refs") or {})
        if (
            str(attempt["task_id"]) != task_id
            or attempt["status"] not in (AttemptState.CREATED, AttemptState.RUNNING)
            or refs.get("runtime_turn_id") not in (None, turn_id)
        ):
            return False
        if attempt["status"] == AttemptState.CREATED:
            await Ledger(repo).attempt_transition(attempt_id, AttemptState.RUNNING)
        refs["runtime_turn_id"] = turn_id
        await repo.update_attempt(attempt_id, refs=refs)
        return True


class MethodToolGuard:
    def __init__(
        self,
        repo: WorkbenchStore,
        *,
        owner: str,
        sandbox: str,
        observe_seconds: float = 2,
    ):
        self.repo, self.owner, self.sandbox = repo, owner, sandbox
        self.observe_seconds = observe_seconds

    async def check(self, name: str, args: dict[str, Any], meta: Any) -> None:
        invocation = await verify_tool_invocation(
            self.repo,
            owner=self.owner,
            sandbox=self.sandbox,
            server=SERVER,
            name=name,
            arguments=args,
            meta=meta,
            observe_seconds=self.observe_seconds,
        )
        turn_id = invocation.turn_id
        deadline = asyncio.get_running_loop().time() + self.observe_seconds
        # Re-read mutable identity after waiting. Other chats may still operate,
        # but cannot name this or any other thread's active task/data session.
        session = await self.repo.get_session(invocation.session_id)
        active = session.get("active_task_id")
        source_attempt = await self.repo.attempt_for_runtime_turn(
            session_id=str(session["id"]), turn_id=turn_id
        )
        if source_attempt and (
            str(active) != str(source_attempt["task_id"])
            or source_attempt["status"]
            not in (AttemptState.CREATED, AttemptState.RUNNING)
        ):
            raise RecordsRefused("this execution is no longer active")
        if args.get("task") and as_id(args["task"], "task") != str(active):
            raise RecordsRefused("the task does not belong to this execution")
        if (
            name != "read_project_conversation"
            and args.get("conversation")
            and (
                session["wheel"] != Wheel.user
                or as_id(args["conversation"], "conversation") != str(session["id"])
            )
        ):
            raise RecordsRefused("the data session does not belong to this execution")
        if not active:
            while not await self.repo.is_accepted_user_turn(
                session_id=str(session["id"]), turn_id=turn_id
            ):
                if asyncio.get_running_loop().time() >= deadline:
                    raise RecordsRefused("this is not an accepted user turn")
                await asyncio.sleep(0.05)
            session = await self.repo.get_session(str(session["id"]))
            if session["wheel"] != Wheel.user or session.get("active_task_id"):
                raise RecordsRefused("the conversation is no longer user-driven")
            return
        task = await self.repo.get_task(str(active), owner_actor_id=self.owner)
        if not is_dynamic(task):
            return
        if (
            session["wheel"] != Wheel.advisor
            or task["state"] != TaskState.RUNNING
            or task.get("cancel_requested_at")
            or task.get("active_interaction_id")
            or not task.get("active_attempt_id")
        ):
            raise RecordsRefused("the method is not running")
        attempt = await self.repo.get_attempt(str(task["active_attempt_id"]))
        while not (attempt.get("refs") or {}).get("runtime_turn_id"):
            if asyncio.get_running_loop().time() >= deadline:
                raise RecordsRefused("the execution turn has not been verified")
            await asyncio.sleep(0.05)
            task = await self.repo.get_task(str(active), owner_actor_id=self.owner)
            if str(task.get("active_attempt_id")) != str(attempt["id"]):
                raise RecordsRefused("the active execution changed")
            attempt = await self.repo.get_attempt(str(attempt["id"]))
        source_attempt = await self.repo.attempt_for_runtime_turn(
            session_id=str(session["id"]), turn_id=turn_id
        )
        if (
            not source_attempt
            or str(source_attempt["id"]) != str(attempt["id"])
            or task["state"] != TaskState.RUNNING
            or task.get("cancel_requested_at")
            or task.get("active_interaction_id")
            or (attempt.get("refs") or {}).get("runtime_turn_id") != turn_id
            or attempt["status"] not in (AttemptState.CREATED, AttemptState.RUNNING)
            or str(attempt["task_id"]) != str(task["id"])
        ):
            raise RecordsRefused("this call is not from the active execution turn")
        if attempt["step_id"] == "data-plan":
            allowed = DATA_PLANNING_TOOLS
        elif (attempt.get("refs") or {}).get("kind") == "method_step":
            view = await execution_for_task(self.repo, task)
            assert view is not None
            step = await active_method_step(self.repo, task, view)
            assert step is not None
            allowed = frozenset(step.tools)
        else:
            allowed = frozenset()  # A plan proposal cannot run any tools.
        if name not in allowed:
            raise RecordsRefused("this tool is not selected for the active method")
        if args.get("conversation") or args.get("scope") == "market":
            raise RecordsRefused("this method may only use its task's data grant")
        if name not in {"describe_items", "lookup"} and args.get("task") != str(
            task["id"]
        ):
            raise RecordsRefused("give the active task id")
