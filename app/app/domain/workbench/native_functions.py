"""Native bindings implemented by this runtime. Availability comes from the center."""

import copy
from typing import Any

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.data_function import DATA_QUERIES, QUERY_PERMISSIONS
from app.domain.workbench.platform_tools import TOOLS
from app.domain.workbench.records import PAGE_ENTRIES
from app.domain.workbench.service_functions import FunctionRevision, ServiceFunction
from app.domain.workbench.tool_registry import ToolRegistration

COMPUTE = "compute"
PROJECT_LIST = "list_project_conversations"
PROJECT_READ = "read_project_conversation"


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


def project_read_revision():
    text = {"type": "string"}
    nullable = {"type": ["string", "null"]}

    def entry(who, **fields):
        props = {"at": nullable, "who": {"enum": who}, **fields}
        return {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        }

    entries = [
        entry(["user", "assistant"], said=text),
        entry(["user"], did={"const": "asked_the_expert"}, expert=nullable),
        entry(
            ["expert"],
            did={"const": "step"},
            step=nullable,
            outcome={"enum": ["accepted", "rejected"]},
            failures={"type": "array", "items": text},
        ),
        entry(["expert"], did={"const": "ended"}, state=text),
        entry(["expert"], did={"const": "asked_the_user"}, about=text),
        entry(
            ["assistant"],
            did={"enum": ["command", "file_change"]},
            brief=text,
            ok={"type": "boolean"},
        ),
        entry(
            ["assistant"],
            did={"const": "tool"},
            name=text,
            brief=text,
            ok={"type": "boolean"},
        ),
    ]
    fields = {
        "conversation": text,
        "kind": text,
        "title": nullable,
        "page": {"type": "integer", "minimum": 1},
        "pages": {"type": "integer", "minimum": 1},
        "entries": {
            "type": "array",
            "maxItems": PAGE_ENTRIES,
            "items": {"oneOf": entries},
        },
    }
    definition = TOOLS[PROJECT_READ]
    contract = ServiceFunction(
        description=definition["description"],
        operation=PROJECT_READ,
        scenes=["native", "step"],
        required_permissions=["project.conversations.content.read"],
        input_schema=definition["inputSchema"],
        output_schema={
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        },
    )
    return FunctionRevision.build("knowbench", PROJECT_READ, 1, contract)


def compute_revision():
    def record(fields):
        return {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        }

    text = {"type": "string"}
    strings = {"type": "array", "items": text}
    input_entry = {
        "oneOf": [
            record({"name": text, "ref": text, "value": text}),
            record({"name": text, "refs": strings, "values": strings}),
        ]
    }
    fields = {
        "id": {"type": "string", "pattern": "^c[1-9][0-9]*$"},
        "name": text,
        "value": text,
        "formula": text,
        "inputs": {"type": "array", "items": input_entry},
    }
    spec = TOOLS[COMPUTE]
    batch = {
        "type": "array",
        "items": record({**fields, "key": text}),
        "minItems": 1,
        "maxItems": spec["inputSchema"]["properties"]["batch"]["maxItems"],
    }
    contract = ServiceFunction(
        description=spec["description"],
        operation=COMPUTE,
        scenes=["native", "step"],
        required_permissions=["task.computations.record"],
        input_schema=spec["inputSchema"],
        output_schema={"oneOf": [record(fields), record({"results": batch})]},
    )
    return FunctionRevision.build("knowbench", COMPUTE, 1, contract)


LOCAL_REVISIONS = {
    COMPUTE: compute_revision,
    PROJECT_LIST: project_list_revision,
    PROJECT_READ: project_read_revision,
}
BINDINGS = {
    **{name: ("knowbench", 1) for name in LOCAL_REVISIONS},
    **{name: ("knowbase", 1) for name in DATA_QUERIES if name != "read_method_input"},
}


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
    props.pop("existing_only", None)
    had_run = props.pop("run", None) is not None
    had_session = props.pop("session", None) is not None
    if had_run:
        props["task"] = _TASK
    if had_session:
        props["conversation"] = _CONVERSATION
    required = [
        "conversation" if r == "session" else r for r in schema.get("required") or []
    ]
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
    if (
        revision.provider == "knowbench"
        and revision != LOCAL_REVISIONS[revision.name]()
    ):
        raise CapabilityError("Local function differs from its registered contract")
    if revision.name in DATA_QUERIES and revision.contract.required_permissions != [
        QUERY_PERMISSIONS[revision.name]
    ]:
        raise CapabilityError("Unsupported data query permissions")
    return ToolRegistration.discover(
        "service_function",
        f"{revision.provider}/{revision.name}/{revision.version}/{revision.digest}",
        descriptor,
    )
