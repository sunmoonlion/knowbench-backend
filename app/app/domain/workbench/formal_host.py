"""Additional operation-bound checks for a pinned formal host action."""

import json

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import (
    FormalTaskSnapshot,
    direct_procedure,
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


def host_call_key(task_id: str) -> str:
    # A replacement Attempt must never create another external action for the
    # same persisted case repetition. A fresh execution requires a fresh Task.
    return "formal-host:" + task_id + ":direct"


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
    if (
        snapshot.caller != "host"
        or call.get("kind") != "tool"
        or call.get("origin") != "managed"
        or call["detail"].get("execution_kind") != "formal_mcp"
        or str(call.get("owner_actor_id")) != str(task["owner_actor_id"])
        or str(call.get("session_id")) != str(task["session_id"])
        or str(call.get("task_id")) != str(task["id"])
        or str(call.get("attempt_id")) != str(attempt["id"])
        or host.get("task_id") != str(task["id"])
        or host.get("attempt_id") != str(attempt["id"])
        or host.get("snapshot_digest") != task["pack_digest"]
        or host.get("slot") != "direct"
        or host.get("run_id") != str(snapshot.run_id)
        or host.get("method_id") != str(snapshot.method_id)
        or host.get("case_id") != snapshot.case_id
        or host.get("repetition") != snapshot.repetition
        or source is None
        or host.get("source_digest") != source.fingerprint()
        or digest(call["detail"].get("arguments")) != digest(snapshot.arguments)
        or call.get("call_key") != host_call_key(str(task["id"]))
        or not call.get("parent_call_id")
    ):
        raise CapabilityError("Formal call evidence belongs to different work")
    if call["digest"] != host_intent_digest(call["detail"]):
        raise CapabilityError("Formal operation evidence changed after approval")
