"""Native bindings implemented by this runtime. Availability comes from the center."""

import copy
from typing import Any

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.platform_tools import TOOLS
from app.domain.workbench.service_functions import FunctionRevision, ServiceFunction
from app.domain.workbench.tool_registry import ToolRegistration

PROJECT_LIST = "list_project_conversations"
BINDINGS = {PROJECT_LIST: ("knowbench", 1), "query_facts": ("knowbase", 1)}


def project_list_revision():
    definition = TOOLS[PROJECT_LIST]
    fields = {
        "conversation": {"type": "string"},
        "kind": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "started_at": {"type": "string"},
        "last_active_at": {"type": ["string", "null"]},
        "turns": {"type": "integer"},
        "first_message": {"type": ["string", "null"]},
        "current": {"type": "boolean"},
    }
    item = {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }
    contract = ServiceFunction(
        description=definition["description"],
        operation=PROJECT_LIST,
        scenes=["native", "step"],
        required_permissions=["project.conversations.read"],
        input_schema=definition["inputSchema"],
        output_schema={
            "type": "object",
            "properties": {"conversations": {"type": "array", "items": item}},
            "required": ["conversations"],
            "additionalProperties": False,
        },
    )
    return FunctionRevision.build("knowbench", PROJECT_LIST, 1, contract)


_TASK = {
    "type": "string",
    "description": "The task id given in the step instructions.",
}
_CONVERSATION = {
    "type": "string",
    "description": "In a chat: the conversation id given in the instructions.",
}


def workbench_data_descriptor(spec: dict[str, Any]) -> dict[str, Any]:
    """knowbase 的工具定义 → 工作台的：run/session 换成 task/conversation。"""
    out = copy.deepcopy(spec)
    schema = out.setdefault("inputSchema", {"type": "object", "properties": {}})
    props = schema.setdefault("properties", {})
    had_run = props.pop("run", None) is not None
    had_session = props.pop("session", None) is not None
    if had_run:
        props["task"] = _TASK
    if had_session:
        props["conversation"] = _CONVERSATION
    required = [r for r in schema.get("required") or [] if r != "session"]
    schema["required"] = ["task" if r == "run" else r for r in required]
    if not schema["required"]:
        schema.pop("required")
    return out


def native_registration(revision):
    descriptor = {
        "name": revision.name,
        "description": revision.contract.description,
        "inputSchema": revision.contract.input_schema,
    }
    if revision.provider == "knowbase":
        descriptor = workbench_data_descriptor(descriptor)
        descriptor["inputSchema"]["additionalProperties"] = False

    if (
        BINDINGS.get(revision.name) != (revision.provider, revision.version)
        or revision.contract.operation != revision.name
        or "native" not in revision.contract.scenes
    ):
        raise CapabilityError("Missing native function binding")
    if revision.name == PROJECT_LIST and revision != project_list_revision():
        raise CapabilityError("Local function differs from its registered contract")
    if revision.name == "query_facts" and revision.contract.required_permissions != [
        "facts.read"
    ]:
        raise CapabilityError("Unsupported fact query permissions")
    return ToolRegistration.discover(
        "service_function",
        f"{revision.provider}/{revision.name}/{revision.version}/{revision.digest}",
        descriptor,
    )
