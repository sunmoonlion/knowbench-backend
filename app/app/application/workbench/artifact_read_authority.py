"""Owner consent to forward pinned task outputs along one real method call path."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.formal_execution import expanded_plan, recursive_plan
from app.domain.workbench.formal_recursive import node_key

EVENT = "method/artifactReadAuthority"


class ArtifactConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: AwareDatetime


class ArtifactRestriction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    retain: list[str] = Field(max_length=100)


async def planned_read(
    repo, task, snapshot, child, attempt, arguments, transfer, *, include_empty=False
):
    """Trace step/caller slot renames, ignoring literals and scheduling-only dependencies.

    Called after step_context verifies every accepted reference and bound value.
    The consent applies to the selected fields only, including across nested experts.
    """
    plan = expanded_plan(snapshot)
    leaf = plan.leaf_nodes[attempt["refs"]["step_index"]]
    by_path = {node.path: node for node in plan.nodes}
    recursive = recursive_plan(snapshot)
    root = await repo.call_by_key(
        str(task["owner_actor_id"]), str(task["session_id"]), "task:" + str(task["id"])
    )
    if root is None:
        raise CapabilityError("Artifact recipient has no root method call")
    parent_id = root["id"]
    legs = []
    for depth in range(1, len(leaf.path) + 1):
        node = by_path[leaf.path[:depth]]
        call = (
            child
            if depth == len(leaf.path)
            else await repo.call_by_key(
                str(task["owner_actor_id"]),
                str(task["session_id"]),
                node_key(task["id"], node.path),
            )
        )
        if call is None or str(call["parent_call_id"]) != str(parent_id):
            raise CapabilityError(
                "Artifact recipient differs from the planned method path"
            )
        parent_id = call["id"]
        refs = call["detail"] if recursive else attempt["refs"]
        legs.append((node, call["id"], refs["input_bindings"]))

    binding = planned_binding(
        task["id"],
        task["owner_actor_id"],
        snapshot,
        root["id"],
        legs,
        arguments,
        transfer,
    )
    return binding if include_empty or binding["fields"] else None


def planned_binding(task_id, owner, snapshot, root_id, legs, arguments, transfer):
    """Derive the same field recipients from live calls or sealed invocation facts."""
    path = [
        {
            "call_id": str(root_id),
            "method_id": str(snapshot.method_id),
            "definition_digest": snapshot.definition_digest,
            "version": None,
        }
    ]
    inputs = []
    for node, call_id, bindings in legs:
        path.append(
            {
                "call_id": str(call_id),
                "method_id": str(node.reference.method_id),
                "version": node.reference.version,
                "definition_digest": node.reference.digest,
            }
        )
        inputs.append((node, {ref["step_id"]: ref for ref in bindings}))
    fields = {}
    for target in sorted(arguments):
        name = target
        for node, artifacts in reversed(inputs):
            binding = node.step.inputs[name].binding
            if binding.source == "caller":
                name = binding.name
                continue
            if binding.source == "step":
                ref = artifacts.get(binding.step)
                if ref is None:
                    raise CapabilityError("Artifact input has no accepted producer")
                fields[target] = {
                    "producer_path": [*node.parent_path, binding.step],
                    "artifact_id": ref["id"],
                    "name": ref["name"],
                    "version": ref["version"],
                    "digest": ref["digest"],
                    "output": binding.output,
                }
            break
    return {
        "task_id": str(task_id),
        "owner_actor_id": str(owner),
        "path": path,
        "fields": fields,
        "purpose": transfer["purpose"],
        "destination": transfer["destination"],
    }


def grant_state(call, binding, expires_at):
    names = sorted(binding["fields"])
    path = binding["path"]
    return {
        "call_id": str(call["id"]),
        "subject_digest": call["digest"],
        "scope_digest": digest(binding),
        "binding": binding,
        "expires_at": expires_at.isoformat(),
        "grants": [
            {
                "call_id": p["call_id"],
                "recipient_call_id": path[i + 1]["call_id"]
                if i + 1 < len(path)
                else None,
                "remaining_depth": len(path) - i - 1,
                "fields": names,
            }
            for i, p in enumerate(path)
        ],
    }


def check(state, call, binding, deadline, *, at=None):
    expected = grant_state(call, binding, datetime.fromisoformat(state["expires_at"]))
    if any(
        state[k] != expected[k]
        for k in ("scope_digest", "subject_digest", "binding", "call_id")
    ):
        raise Forbidden("Artifact consent differs from the pending invocation")
    expiry = datetime.fromisoformat(state["expires_at"])
    if not (at or datetime.now(UTC)) < expiry <= deadline:
        raise Forbidden("Artifact read consent has expired")
    grants = state["grants"]
    if len(grants) != len(expected["grants"]):
        raise Forbidden("Artifact delegation path changed")
    available = set(binding["fields"])
    for grant, original in zip(grants, expected["grants"], strict=True):
        if {**grant, "fields": original["fields"]} != original:
            raise Forbidden("Artifact delegation path changed")
        forwarded = set(grant["fields"])
        if not forwarded <= available:
            raise Forbidden("Artifact permission exceeds its parent")
        available = forwarded
    if available != set(binding["fields"]):
        raise Forbidden("An input is outside the current artifact read permission")


async def record(repo, task, call, state):
    await repo.append_event(
        session_id=str(task["session_id"]),
        task_id=str(task["id"]),
        attempt_id=str(call["attempt_id"]),
        kind="task",
        event_type=EVENT,
        payload=state,
    )


def deadline_for(interaction, scopes):
    return min([interaction["expires_at"], *(s.expires_at for s in scopes or ())])


async def authorize_dispatch(repo, task, call, binding, scopes):
    if not binding:
        return None
    interaction = await repo.get_interaction(str(call["interaction_id"]))
    deadline = deadline_for(interaction, scopes)
    state = await repo.latest_call_event(call, EVENT)
    if state is None:
        displayed = interaction["prompt"].get("subject", {}).get("artifact_read")
        if displayed != binding or call["detail"].get("artifact_read_digest") != digest(
            binding
        ):
            raise Forbidden(
                "Confirm the exact artifact read before approving the action"
            )
        state = grant_state(call, binding, deadline)
        await record(repo, task, call, state)
    check(state, call, binding, deadline)
    return state


async def verify_approval(
    repo, task, attempt, call, binding, interaction, confirmation, *, deadline=None
):
    if str(call["task_id"]) != str(task["id"]) or str(call["attempt_id"]) != str(
        attempt["id"]
    ):
        raise ValueError("artifact_read_invocation_mismatch")
    state = await repo.latest_call_event(call, EVENT, before=confirmation["cursor"])
    intent = call["detail"]
    subject = interaction["prompt"]["subject"]
    legacy = "artifact_read_digest" not in intent and "artifact_read" not in subject
    if (
        not binding
        or not state
        or digest(state) != confirmation["payload"].get("artifact_read_digest")
        or (
            not legacy
            and (
                intent.get("artifact_read_digest") != digest(binding)
                or subject.get("artifact_read") != binding
            )
        )
    ):
        raise ValueError("artifact_read_approval_mismatch")
    check(
        state,
        call,
        binding,
        deadline or interaction["expires_at"],
        at=interaction["consumed_at"],
    )
    return {
        "scope_digest": digest(binding),
        "grant_digest": digest(state),
        "binding": binding,
    }


async def manage(
    svc, owner, call_id, *, consent=None, restriction=None, node_id: UUID | None = None
):
    from app.application.workbench.formal_host_calls import FormalHostCalls

    repo = svc.repo
    async with repo.transaction():
        peek = await repo.capability_call(owner, str(call_id))
        if not peek.get("task_id"):
            raise CapabilityError("This action has no method task")
        task = await repo.get_task(
            str(peek["task_id"]), owner_actor_id=owner, for_update=True
        )
        call = await repo.capability_call(owner, str(call_id), lock=True)
        if call["status"] != "awaiting_approval":
            raise CapabilityError("The artifact action is no longer pending")
        formal = FormalHostCalls(svc)
        host = formal.pending_invocation(task, call)
        ctx = await formal.approval_context(owner, call, host)
        binding = ctx.artifact_read
        if not binding:
            raise CapabilityError("This action has no planned artifact read")
        interaction = await repo.get_interaction(str(call["interaction_id"]))
        deadline = deadline_for(interaction, ctx.scopes)
        scope_digest = digest(binding)
        state = await repo.latest_call_event(call, EVENT)
        if consent is not None:
            if (
                consent.scope_digest != scope_digest
                or not datetime.now(UTC) < consent.expires_at <= deadline
            ):
                raise Forbidden(
                    "Artifact consent differs from the displayed scope or deadline"
                )
            proposed = grant_state(call, binding, consent.expires_at)
            if state is None:
                state = proposed
                await record(repo, task, call, state)
            elif state != proposed:
                raise Forbidden("An existing artifact grant cannot be replaced")
        if restriction is not None:
            if (
                restriction.scope_digest != scope_digest
                or state is None
                or state["binding"] != binding
            ):
                raise Forbidden("No matching artifact grant to narrow")
            grants = state["grants"]
            index = next(
                (i for i, g in enumerate(grants) if g["call_id"] == str(node_id)), None
            )
            keep = set(restriction.retain)
            if index is None or not keep <= set(grants[index]["fields"]):
                raise Forbidden("An artifact grant may only be narrowed")
            narrowed = [
                *grants[:index],
                *[
                    {**g, "fields": sorted(keep.intersection(g["fields"]))}
                    for g in grants[index:]
                ],
            ]
            if narrowed != grants:
                state = {**state, "grants": narrowed}
                await record(repo, task, call, state)
        return {
            "scope_digest": scope_digest,
            "binding": binding,
            "expires_at": deadline,
            "authority": state,
        }
