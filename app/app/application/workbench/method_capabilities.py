"""Resolve the active frozen step using the existing task/plan/attempt ledger."""

from app.application.workbench.execution_plan import (
    active_method_step,
    display_for_task,
    execution_for_task,
)
from app.domain.workbench.capabilities import CapabilityError, digest, tool_key
from app.domain.workbench.method_capabilities import (
    MethodCapabilities,
    matches_skill,
    matches_tool,
)
from app.domain.workbench.method_plan import PLAN_STEP
from app.domain.workbench.states import ATTEMPT_TERMINAL, TaskState


async def method_scope(repo, owner, session, *, lock=False):
    if session["wheel"] == "user" or not session.get("active_task_id"):
        return None
    task = await repo.get_task(
        str(session["active_task_id"]), owner_actor_id=owner, for_update=lock
    )
    pack = await display_for_task(repo, task)
    step = pack.step(int(task["current_step"])) if pack else None
    attempt = (
        await repo.get_attempt(str(task["active_attempt_id"]))
        if task.get("active_attempt_id")
        else None
    )
    special = bool(
        attempt
        and attempt["status"] not in ATTEMPT_TERMINAL
        and attempt["step_id"] in ("data-plan", PLAN_STEP)
    )
    requirements = step.managed_capabilities if step and not special else None
    context = digest(
        {
            "task": str(task["id"]),
            "plan": task.get("accepted_plan_version"),
            "step": step.model_dump(mode="json") if step and not special else None,
            "special": attempt["step_id"] if special and attempt else None,
        }
    )
    editable = bool(
        requirements is not None
        and not task.get("cancel_requested_at")
        and task["state"]
        in (TaskState.RECEIVED, TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING)
        and (not attempt or attempt["status"] in ATTEMPT_TERMINAL)
    )
    return {
        "task": task,
        "step": step,
        "attempt": attempt,
        "special": special,
        "requirements": requirements or MethodCapabilities(),
        "access_requirements": step.access_requirements if step and not special else (),
        "context": context,
        "editable": editable,
    }


def selection_issues(scope, detail, assets):
    requirements = scope["requirements"]
    chosen_tools = set(detail.get("tools", []))
    chosen_skills = set(detail.get("skills", []))
    found_tools, found_skills, issues = set(), set(), []
    for asset in assets:
        if asset["kind"] == "connection":
            for tool in asset["detail"].get("tools", []):
                key = tool_key(asset["id"], tool["name"])
                if key not in chosen_tools:
                    continue
                if not matches_tool(requirements, asset, tool):
                    issues.append({"ref": key, "code": "outside_method_scope"})
                found_tools.add((asset["detail"]["url"], tool["name"]))
        elif asset["id"] in chosen_skills:
            if not matches_skill(requirements, asset, assets):
                issues.append({"ref": asset["id"], "code": "outside_method_scope"})
            found_skills.add(asset["name"])
    if found_tools != {(r.url, r.name) for r in requirements.tools} or found_skills != {
        r.name for r in requirements.skills
    }:
        issues.append({"ref": "method", "code": "method_bindings_required"})
    if len(chosen_tools) != len(found_tools) or len(chosen_skills) != len(found_skills):
        issues.append({"ref": "method", "code": "ambiguous_method_binding"})
    return issues


async def check_runtime_method(repo, owner, session, runtime, *, lock=False):
    binding = runtime.get("method")
    scope = await method_scope(repo, owner, session, lock=lock)
    if not binding:
        if scope or session["wheel"] != "user":
            raise CapabilityError("Capability scope is not valid for the active method")
        return
    if not scope:
        raise CapabilityError("Method execution no longer owns this session")
    task, attempt, step = scope["task"], scope["attempt"], scope["step"]
    if (
        scope["context"] != binding["context"]
        or not attempt
        or not step
        or str(attempt["id"]) != binding["attempt_id"]
        or attempt["step_id"] != step.step_id
        or attempt["step_version"] != step.step_version
        or attempt["status"] in ATTEMPT_TERMINAL
        or task.get("cancel_requested_at")
        or task["state"] not in (TaskState.RUNNING, TaskState.WAITING)
    ):
        raise CapabilityError("Method step or execution authorization changed")
    view = await execution_for_task(repo, task)
    if (
        view is None
        or await active_method_step(repo, {**task, "state": TaskState.RUNNING}, view)
        != step
    ):
        raise CapabilityError("Method execution differs from its accepted plan")
    if task["state"] == TaskState.WAITING:
        if not task.get("active_interaction_id"):
            raise CapabilityError("Method is waiting outside the current operation")
        interaction = await repo.get_interaction(str(task["active_interaction_id"]))
        subject = (interaction.get("prompt") or {}).get("subject") or {}
        if (
            not subject.get("capability_call_id")
            or str(interaction.get("task_id")) != str(task["id"])
            or str(interaction.get("attempt_id")) != str(attempt["id"])
        ):
            raise CapabilityError("Method is waiting outside the current operation")
