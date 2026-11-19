"""Identity and bounded execution rules for a model incubation probe."""

from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.formal_execution import snapshot_for_task
from app.domain.workbench.projects import CODEX_VERSION

PROBE_TOOL = "formal_probe_call"
PROBE_SERVER = "sunmoon_capabilities"
PROBE_ISOLATION = f"codex-{CODEX_VERSION}/formal-probe-v1"
_PREVIOUS_PROBE_CONFIG = {
    "features.shell_tool": False,
    "features.multi_agent": False,
    "features.apps": False,
    "features.goals": False,
    "features.default_mode_request_user_input": False,
    "web_search": "disabled",
}


PROBE_CONFIG = {**_PREVIOUS_PROBE_CONFIG, "features.view_image": False}


def verified_probe_profile(evidence: dict) -> bool:
    # Frozen methods carry the exact profile observed when their test ran.
    version = evidence.get("codex_version")
    if version == CODEX_VERSION:
        config = PROBE_CONFIG
    elif version == "0.155.1":
        config = _PREVIOUS_PROBE_CONFIG
    else:
        return False
    return (
        evidence.get("version") == f"codex-{version}/formal-probe-v1"
        and evidence.get("applied_config") == config
    )


def model_call_key(task_id: str) -> str:
    return "formal-model:" + task_id + ":direct"


def model_intent_digest(intent: dict) -> str:
    return digest(
        {
            k: v
            for k, v in intent.items()
            if k not in ("approval_token", "authorization", "result")
        }
    )


def validate_model_call(task: dict, attempt: dict, call: dict) -> None:
    snapshot = snapshot_for_task(task)
    context = call["detail"].get("formal_probe") or {}
    invocation = call["detail"].get("invocation") or {}
    source = snapshot.draft.source
    refs = attempt.get("refs") or {}
    if (
        snapshot.caller != "model"
        or source is None
        or call.get("kind") != "tool"
        or call.get("origin") != "managed"
        or call["detail"].get("execution_kind") != "formal_probe"
        or str(call.get("owner_actor_id")) != str(task["owner_actor_id"])
        or str(call.get("session_id")) != str(task["session_id"])
        or str(call.get("task_id")) != str(task["id"])
        or str(call.get("attempt_id")) != str(attempt["id"])
        or context
        != {
            "task_id": str(task["id"]),
            "attempt_id": str(attempt["id"]),
            "snapshot_digest": task["pack_digest"],
            "slot": "direct",
            "method_id": str(snapshot.method_id),
            "run_id": str(snapshot.run_id),
            "case_id": snapshot.case_id,
            "repetition": snapshot.repetition,
            "source_digest": source.fingerprint(),
        }
        or invocation.get("session_id") != str(task["session_id"])
        or invocation.get("thread_id") != task.get("thread_id")
        or invocation.get("turn_id") != refs.get("runtime_turn_id")
        or invocation.get("server") != PROBE_SERVER
        or not isinstance(invocation.get("call_id"), str)
        or invocation.get("key")
        != digest(
            [
                invocation.get("session_id"),
                invocation.get("thread_id"),
                invocation.get("turn_id"),
                PROBE_SERVER,
                invocation.get("call_id"),
            ]
        )
        or (refs.get("formal_model_probe") or {}).get("invocation_key")
        != invocation.get("key")
        or digest(call["detail"].get("arguments")) != digest(snapshot.arguments)
        or call.get("call_key") != model_call_key(str(task["id"]))
        or not call.get("parent_call_id")
        or call["digest"] != model_intent_digest(call["detail"])
    ):
        raise CapabilityError("Formal model invocation evidence differs from its test")


@dataclass
class FormalProbeGuard:
    remaining: Decimal
    failure: str | None = None
    seen: set[str] = field(default_factory=set)

    def observe(self, item):
        kind = item.get("type")
        if kind in (
            "commandExecution",
            "webSearch",
            "fileChange",
            "collabAgentToolCall",
            "dynamicToolCall",
        ):
            self.failure = "unexpected_tool"
        if kind != "mcpToolCall":
            return
        if item.get("server") != PROBE_SERVER or item.get("tool") != PROBE_TOOL:
            self.failure = "unexpected_tool"
            return
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            self.failure = "invalid_tool_identity"
            return
        self.seen.add(call_id)
        if len(self.seen) > 1:
            self.failure = "method_call_limit"

    def usage(self, cost):
        if cost is None:
            self.failure = "model_cost_unknown"
        elif Decimal(str(cost)) >= self.remaining:
            self.failure = "budget_limit"
