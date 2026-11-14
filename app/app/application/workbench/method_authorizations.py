"""Persist and recheck host-bound authority using the existing confirmation flow."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.method_calls import root_call
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    PermissionGrant,
    check_delegation,
    delegate,
)
from app.domain.workbench.states import TASK_TERMINAL


async def confirm_method_permissions(
    repo: WorkbenchStore, owner: str, scope: dict, access: dict, assets_by_tool: dict
) -> dict:
    """Called only by the owner-confirmed, context/revision-checked selection save.

    Grants apply to the displayed method step only. Broader task binding and
    reusable account preferences remain separate from this confirmation.
    """
    task = scope["task"]
    root = await root_call(repo, task)
    previous = await repo.method_authorization(owner, root["id"], lock=True)
    current = MethodAuthorization.model_validate(previous) if previous else None
    expiry = datetime.now(UTC) + timedelta(hours=24)
    method_id = scope["step"].step_id
    granted = tuple(
        PermissionGrant(
            grant_id=digest([scope["context"], key]),
            confirmation_context=scope["context"],
            connection_id=UUID(assets_by_tool[key]["id"]),
            connection_revision=assets_by_tool[key]["revision"],
            operation_digest=value["operation_digest"],
            requirement=value["requirement"],
            delegable=True,
            expires_at=expiry,
            method_ids=(method_id,),
        )
        for key, value in access.items()
    )
    retained = tuple(
        g
        for g in (current.grants if current and not current.revoked else ())
        if g.confirmation_context != scope["context"]
        and g.expires_at > datetime.now(UTC)
    )
    authorization = MethodAuthorization(
        call_id=UUID(root["id"]),
        revision=(current.revision + 1) if current else 1,
        parent_call_id=None,
        parent_revision=None,
        method_id=str(task["profile_id"]),
        revoked=False,
        expires_at=expiry,
        grants=(*retained, *granted),
    )
    await repo.save_method_authorization(
        owner,
        str(task["session_id"]),
        authorization.model_dump(mode="json"),
        previous=current.revision if current else 0,
    )
    # Explicit reconfirmation may replace this child's expired/narrowed scope.
    # Merely widening a parent later still cannot change any existing child.
    from app.application.workbench.execution_plan import _meta, execution_for_task
    from app.application.workbench.method_calls import step_call

    view = await execution_for_task(repo, task)
    if view is not None and view.receipt is not None:
        call = await step_call(repo, task, _meta(view, int(task["current_step"])))
        previous_child = await repo.method_authorization(owner, call["id"], lock=True)
        child = delegate(
            authorization,
            call_id=UUID(call["id"]),
            method_id=method_id,
            requirements=granted,
        )
        if previous_child:
            child = child.model_copy(
                update={"revision": previous_child["revision"] + 1}
            )
        await repo.save_method_authorization(
            owner,
            str(task["session_id"]),
            child.model_dump(mode="json"),
            previous=previous_child["revision"] if previous_child else 0,
        )
    await repo.append_event(
        session_id=str(task["session_id"]),
        task_id=str(task["id"]),
        kind="task",
        event_type="method/permissionsConfirmed",
        payload={
            "authorization": authorization.model_dump(mode="json"),
            "context": scope["context"],
        },
    )
    return {
        "call_id": root["id"],
        "revision": authorization.revision,
        "grants": [g.grant_id for g in granted],
    }


async def permission_status(
    repo: WorkbenchStore, owner: str, scope: dict, detail: dict
) -> str:
    """Read-only projection of the actual scope, including the frozen child.

    An old step's confirmation or a renewed root is not confirmation of another
    step. Never create a method call or renew authority while rendering a page.
    """
    if not (scope["requirements"].tools or scope["requirements"].skills):
        return "not_required"
    confirmation = detail.get("authorization") or {}
    task = scope["task"]
    root = await repo.call_by_key(
        owner, str(task["session_id"]), "task:" + str(task["id"])
    )
    if root is None or confirmation.get("call_id") != root["id"]:
        return "unconfirmed"
    raw = await repo.method_authorization(owner, root["id"])
    if raw is None:
        return "unconfirmed"
    parent = MethodAuthorization.model_validate(raw)
    now = datetime.now(UTC)
    if parent.revoked:
        return "revoked"
    if parent.expires_at <= now:
        return "expired"
    wanted = set(confirmation.get("grants", []))
    grants = {
        g.grant_id: g
        for g in parent.grants
        if g.confirmation_context == scope["context"]
    }
    if not wanted <= grants.keys():
        return "unconfirmed"
    if any(grants[key].expires_at <= now for key in wanted):
        return "expired"
    refs = (scope.get("attempt") or {}).get("refs") or {}
    if refs.get("method_call_id"):
        child_raw = await repo.method_authorization(owner, refs["method_call_id"])
        if child_raw is None:
            return "unconfirmed"
        child = MethodAuthorization.model_validate(child_raw)
        if child.revoked:
            return "revoked"
        if child.expires_at <= now:
            return "expired"
        try:
            check_delegation(parent, child, now=now)
        except CapabilityError:
            return "unconfirmed"
    return "active"


async def bind_method_permissions(
    repo: WorkbenchStore, owner: str, scope: dict, selected: dict
) -> str:
    """Bind the accepted-plan method invocation, not a caller-supplied parent ID."""
    from app.application.workbench.execution_plan import _meta, execution_for_task
    from app.application.workbench.method_calls import step_call

    task = scope["task"]
    confirmation = selected.get("authorization")
    root = await root_call(repo, task)
    if not confirmation or confirmation.get("call_id") != root["id"]:
        raise CapabilityError("Confirm this method's permissions before execution")
    raw = await repo.method_authorization(owner, root["id"], lock=True)
    if raw is None:
        raise CapabilityError("Method authorization is unavailable")
    parent = MethodAuthorization.model_validate(raw)
    if parent.revoked or parent.expires_at <= datetime.now(UTC):
        raise CapabilityError("Method authorization expired or was revoked")
    allowed = set(confirmation["grants"])
    grants = tuple(g for g in parent.grants if g.grant_id in allowed)
    if len(grants) != len(allowed) or any(
        g.confirmation_context != scope["context"] for g in grants
    ):
        raise CapabilityError("Method permission confirmation changed")
    view = await execution_for_task(repo, task)
    if view is None or view.receipt is None:
        # A fixed workflow has no independently registered method children.
        return root["id"]
    call = await step_call(repo, task, _meta(view, int(task["current_step"])))
    existing = await repo.method_authorization(owner, call["id"], lock=True)
    if existing:
        child = MethodAuthorization.model_validate(existing)
        check_delegation(parent, child)
        if {g.grant_id for g in child.grants} != allowed:
            raise CapabilityError("Method authorization changed; reconfirm the step")
    else:
        child = delegate(
            parent,
            call_id=UUID(call["id"]),
            method_id=scope["step"].step_id,
            requirements=grants,
        )
        await repo.save_method_authorization(
            owner,
            str(task["session_id"]),
            child.model_dump(mode="json"),
            previous=0,
        )
    return call["id"]


async def check_method_permissions(
    repo: WorkbenchStore, owner: str, scope: dict, runtime: dict
) -> None:
    """Every dispatch, approval and recovery reads live persisted ancestors."""
    task = scope["task"]
    if task["state"] in TASK_TERMINAL or task.get("cancel_requested_at"):
        raise CapabilityError("Method execution no longer has authorization")
    root = await repo.call_by_key(
        owner, str(task["session_id"]), "task:" + str(task["id"])
    )
    if root is None:
        raise CapabilityError("Method invocation is unavailable")
    binding = runtime["method"]
    current_id = binding.get("call_id")
    if not isinstance(current_id, str):
        raise CapabilityError("Method permission binding is missing")
    refs = (scope.get("attempt") or {}).get("refs") or {}
    expected = (
        refs.get("method_call_id") if refs.get("kind") == "method_step" else root["id"]
    )
    if current_id != expected:
        raise CapabilityError(
            "Method permission does not belong to the active invocation"
        )
    seen = set()
    child = None
    for _ in range(16):
        if current_id in seen:
            raise CapabilityError("Cyclic method authorization")
        seen.add(current_id)
        raw = await repo.method_authorization(owner, current_id)
        if raw is None:
            raise CapabilityError("Method permission is unavailable")
        current = MethodAuthorization.model_validate(raw)
        if current.revoked or current.expires_at <= datetime.now(UTC):
            raise CapabilityError("Method permission expired or was revoked")
        if child is None:
            grants = {g.grant_id: g for g in current.grants}
            for key, spec in runtime.get("tools", {}).items():
                if spec.get("execution_kind") == "skill_script":
                    continue
                grant = grants.get(digest([scope["context"], key]))
                if (
                    grant is None
                    or grant.expires_at <= datetime.now(UTC)
                    or str(grant.connection_id) != spec.get("connection_id")
                    or grant.connection_revision != spec.get("revision")
                    or grant.operation_digest != spec.get("operation_digest")
                    or {
                        "requirement": grant.requirement.model_dump(mode="json"),
                        "operation_digest": grant.operation_digest,
                    }
                    != spec.get("access")
                ):
                    raise CapabilityError(
                        "Current method permission no longer covers this operation"
                    )
        if child is not None:
            check_delegation(current, child)
        if current.parent_call_id is None:
            if current_id != root["id"]:
                raise CapabilityError("Method permission belongs to another task")
            return
        child = current
        current_id = str(current.parent_call_id)
    raise CapabilityError("Method permission depth exceeds the limit")
