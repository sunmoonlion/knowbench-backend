"""Recheck the consumed formal approval at the adapter's send boundary."""

from datetime import UTC, datetime

from app.application.workbench.call_policy import authorize_operation
from app.application.workbench.tool_registry import registered_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    check_delegation,
)
from app.domain.workbench.states import TaskState


async def check_formal_dispatch(svc, owner, call, asset, intent):
    repo = svc.repo
    if intent.get("execution_kind") in ("formal_method", "formal_method_model"):
        await svc.require_formal_use(owner, intent["formal_method"]["reference"])
    if asset["status"] != "active" or asset["revision"] != intent["revision"]:
        raise CapabilityError("The approved connection changed")
    auth = intent.get("authorization") or {}
    raw = await repo.method_authorization(owner, auth.get("call_id"))
    if raw is None:
        raise CapabilityError("Formal action authorization is unavailable")
    current = MethodAuthorization.model_validate(raw)
    now = datetime.now(UTC)
    host = intent.get("formal_host") or intent.get("formal_probe")
    origin = call["id"] if host else call["parent_call_id"]
    grant_id = digest([origin, call["digest"]])
    grant = next((g for g in current.grants if g.grant_id == grant_id), None)
    if (
        not grant
        or str(current.call_id) != auth.get("call_id")
        or grant.recipient_call_id != current.call_id
        or current.revoked
        or current.revision != auth.get("revision")
        or current.expires_at <= now
        or grant.expires_at <= now
        or not grant.matches_asset(
            "skill_script"
            if intent.get("execution_kind") == "formal_script"
            else "mcp",
            asset["id"],
            asset["revision"],
        )
    ):
        raise CapabilityError("Formal action authorization changed or expired")
    ancestor = current
    seen = {ancestor.call_id}
    while ancestor.parent_call_id:
        if ancestor.parent_call_id in seen:
            raise CapabilityError("Formal authorization ancestry is invalid")
        seen.add(ancestor.parent_call_id)
        parent = await repo.method_authorization(owner, str(ancestor.parent_call_id))
        if parent is None:
            raise CapabilityError("Formal parent authorization is unavailable")
        parent = MethodAuthorization.model_validate(parent)
        check_delegation(parent, ancestor, now=now)
        ancestor = parent
    if intent.get("execution_kind") == "formal_script":
        from app.application.workbench.formal_host_calls import FormalHostCalls

        context = await FormalHostCalls(svc).context(
            owner, host["task_id"], host["attempt_id"]
        )
        reservations = await repo.script_budget_entries(call["id"])
        if (
            context.identity != call["digest"]
            or not svc.script_executor
            or reservations.get("reserve", {}).get("script_evidence")
            != intent.get("script_quote")
            or "consume" in reservations
        ):
            raise CapabilityError(
                "Formal script approval or root budget reservation changed"
            )
        registration = context.registration
    else:
        registration = registered_call(asset, intent)
    decision = authorize_operation(
        registration,
        intent["arguments"],
        operations=svc.operations,
        method=True,
        access=intent["access"],
        operation_digest=grant.operation_digest,
    )
    if decision.operation != intent.get("operation"):
        raise CapabilityError("Formal action contents changed before dispatch")
    session = await repo.get_session(call["session_id"], owner_actor_id=owner)
    if intent.get("execution_kind") in ("formal_method_model", "formal_probe"):
        selected = await repo.capability_selection(owner, call["session_id"])
        runtime = selected["runtime"]
        invocation = intent["invocation"]
        if (
            runtime.get("status") != "applied"
            or runtime.get("aborted")
            or runtime.get("token_hash") != intent["runtime"]
            or selected["revision"] != intent["selection_revision"]
            or runtime.get("selection_revision") != selected["revision"]
            or runtime.get("turn_id") != invocation["turn_id"]
            or datetime.fromisoformat(runtime["expires_at"]) <= now
        ):
            raise CapabilityError("Formal model turn changed before dispatch")
        if intent.get("formal_method") and (
            session.get("thread_id") != invocation["thread_id"]
            or runtime.get("scope_id") != intent["formal_method"]["scope_id"]
        ):
            raise CapabilityError("Formal conversation scope changed before dispatch")
    if host:
        task = await repo.get_task(host["task_id"], owner_actor_id=owner)
        if (task.get("pack_snapshot") or {}).get("bindings") and intent.get(
            "execution_kind"
        ) != "formal_script":
            from app.application.workbench.formal_host_calls import FormalHostCalls

            context = await FormalHostCalls(svc).context(
                owner, host["task_id"], host["attempt_id"]
            )
            if context.identity != call["digest"]:
                raise CapabilityError(
                    "Incubation tool authority changed before dispatch"
                )
        if (
            str(session.get("active_task_id")) != host["task_id"]
            or task["state"] != TaskState.RUNNING
            or task.get("cancel_requested_at")
            or str(task.get("active_attempt_id")) != host["attempt_id"]
        ):
            raise CapabilityError("Formal task changed before dispatch")
    elif session.get("active_task_id") or session["wheel"] != "user":
        raise CapabilityError("Formal conversation changed before dispatch")
