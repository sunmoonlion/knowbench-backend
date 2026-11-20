"""Additional operation-bound checks for a pinned formal host action."""

import json

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_adapter import host_kind
from app.domain.workbench.formal_execution import (
    FormalTaskSnapshot,
    dependency_for_step,
    direct_procedure,
    execution_slot,
    execution_steps,
    expanded_plan,
    recursive_plan,
    validate_values,
)


def check_host_inputs(snapshot: FormalTaskSnapshot) -> None:
    try:
        if snapshot.caller != "host":
            raise ValueError("A model probe cannot use the direct host entry")
        procedure = direct_procedure(snapshot.draft)
        if len(snapshot.draft.definition.access_requirements) != 1:
            raise ValueError(
                "Host execution requires one complete operation permission"
            )
        encoded = json.dumps(snapshot.arguments, allow_nan=False).encode()
        if len(encoded) > 64000:
            raise ValueError("Oversized formal arguments")
        validate_values(procedure.inputs, snapshot.arguments)
    except (ValueError, TypeError, RecursionError) as exc:
        raise CapabilityError(
            "Arguments or permissions do not match the supported formal operation"
        ) from exc


def host_call_key(task_id: str, slot: str = "direct") -> str:
    # A replacement Attempt must never create another external action for the
    # same persisted case repetition. A fresh execution requires a fresh Task.
    return "formal-host:" + task_id + ":" + slot


def host_intent_digest(intent: dict) -> str:
    original = {
        k: v
        for k, v in intent.items()
        if k not in ("approval_token", "authorization", "result")
    }
    host = dict(original["formal_host"])
    host.pop("attempt_id")
    return digest({**original, "formal_host": host})


def validate_host_call(task: dict, attempt: dict, call: dict) -> None:
    from app.domain.workbench.formal_execution import snapshot_for_task

    snapshot = snapshot_for_task(task)
    host = call["detail"].get("formal_host") or {}
    source = snapshot.draft.source
    refs = attempt.get("refs") or {}
    slot, method_id, arguments_digest = (
        "direct",
        str(snapshot.method_id),
        digest(snapshot.arguments),
    )
    if snapshot.dependencies:
        steps = execution_steps(snapshot)
        index = refs.get("step_index")
        if not isinstance(index, int) or not 0 <= index < len(steps):
            raise CapabilityError("Formal step index is invalid")
        step = steps[index]
        frozen = dependency_for_step(snapshot, step)
        source, slot, method_id = (
            snapshot.execution_source(frozen),
            execution_slot(snapshot, index),
            str(frozen.reference.method_id),
        )
        arguments_digest = refs.get("arguments_digest")
        if (
            refs.get("kind") != "formal_orchestration_step"
            or refs.get("slot") != slot
            or refs.get("reference") != frozen.reference.model_dump(mode="json")
            or host.get("reference") != refs["reference"]
            or host.get("parent_method_id")
            != (
                str(
                    next(
                        n.reference.method_id
                        for n in expanded_plan(snapshot).nodes
                        if n.path
                        == expanded_plan(snapshot).leaf_nodes[index].parent_path
                    )
                )
                if recursive_plan(snapshot)
                and expanded_plan(snapshot).leaf_nodes[index].parent_path
                else str(snapshot.method_id)
            )
            or host.get("method_call_id") != refs.get("method_call_id")
            or str(call.get("parent_call_id")) != refs.get("method_call_id")
            or host.get("inputs_digest") != arguments_digest
            or not arguments_digest
        ):
            raise CapabilityError("Formal dependency invocation changed")
        if recursive_plan(snapshot):
            from app.domain.workbench.formal_recursive import TREE_ADAPTER

            node = expanded_plan(snapshot).leaf_nodes[index]
            if (
                refs.get("path") != list(node.path)
                or host.get("path") != list(node.path)
                or host.get("adapter") != TREE_ADAPTER
                or host.get("parent_method_call_id")
                != refs.get("parent_method_call_id")
                or host.get("root_method_call_id") != refs.get("root_method_call_id")
            ):
                raise CapabilityError("Formal recursive call path changed")

    if (
        snapshot.caller != "host"
        or call.get("kind") != "tool"
        or call.get("origin") != "managed"
        or source is None
        or call["detail"].get("execution_kind") != host_kind(source)
        or str(call.get("owner_actor_id")) != str(task["owner_actor_id"])
        or str(call.get("session_id")) != str(task["session_id"])
        or str(call.get("task_id")) != str(task["id"])
        or str(call.get("attempt_id")) != str(attempt["id"])
        or host.get("task_id") != str(task["id"])
        or host.get("attempt_id") != str(attempt["id"])
        or host.get("snapshot_digest") != task["pack_digest"]
        or host.get("slot") != slot
        or host.get("run_id") != str(snapshot.run_id)
        or host.get("method_id") != method_id
        or host.get("case_id") != snapshot.case_id
        or host.get("repetition") != snapshot.repetition
        or source is None
        or host.get("source_digest") != source.fingerprint()
        or digest(call["detail"].get("arguments")) != arguments_digest
        or call.get("call_key") != host_call_key(str(task["id"]), slot)
        or not call.get("parent_call_id")
    ):
        raise CapabilityError("Formal call evidence belongs to different work")
    if call["digest"] != host_intent_digest(call["detail"]):
        raise CapabilityError("Formal operation evidence changed after approval")
