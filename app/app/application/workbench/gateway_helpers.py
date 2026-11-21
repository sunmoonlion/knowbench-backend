"""Registered resource and receipt readers on the session-scoped MCP gateway."""

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.service_functions import FunctionRevision, ServiceFunction
from app.domain.workbench.skill_package import SkillRead
from app.domain.workbench.tool_schema import schema_validator


def _object(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def helper_revisions():
    text = {"type": "string"}
    index = {"type": "integer", "minimum": 0}
    skill = {"skill_id": text, "version": text}
    metadata = {"path": text, "size": index, "sha256": text, "media_type": text}
    definitions = (
        (
            "action_result",
            "session.action_result.read",
            "Read the outcome of a previously requested external action in this conversation.",
            _object({"call_id": text}, ["call_id"]),
            _object(
                {"call_id": text, "status": text, "result": {}},
                ["call_id", "status", "result"],
            ),
        ),
        (
            "skill_files",
            "session.skill_files.list",
            "List files in a selected Skill package. Returns 50 paths per page; offset is a file index.",
            _object({"skill_id": text, "offset": index}, ["skill_id"]),
            _object(
                {
                    **skill,
                    "files": {
                        "type": "array",
                        "maxItems": 50,
                        "items": _object(metadata, list(metadata)),
                    },
                    "next_offset": {"type": ["integer", "null"]},
                },
                ["skill_id", "version", "files", "next_offset"],
            ),
        ),
        (
            "skill_read",
            "session.skill_resource.read",
            "Read a resource from a selected Skill. Use package paths from skill_files or relative_to. Text offsets count characters.",
            SkillRead.model_json_schema(),
            _object(
                {
                    **skill,
                    **metadata,
                    "image_base64": text,
                    "message": text,
                    "text": text,
                    "offset": index,
                    "next_offset": {"type": ["integer", "null"]},
                    "connection_dependencies": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
                list(skill) + list(metadata),
            ),
        ),
    )
    return {
        name: FunctionRevision.build(
            "knowbench",
            name,
            1,
            ServiceFunction(
                operation=name,
                scenes=["native", "step"],
                required_permissions=[permission],
                description=description,
                input_schema={**inputs, "additionalProperties": False},
                output_schema=outputs,
            ),
        )
        for name, permission, description, inputs, outputs in definitions
    }


async def registered_helpers(repo, names):
    if not names:
        return {}
    expected = helper_revisions()
    bindings = {name: ("knowbench", 1) for name in names if name in expected}
    rows = await repo.scene_function_revisions(bindings, scene="native")
    return {r.name: r for r in rows if r == expected.get(r.name)}


async def available_helpers(repo, runtime, tools):
    pins = runtime.get("helper_digests", {})
    names = []
    if tools and not runtime.get("formal_probe"):
        names.append("action_result")
    if runtime.get("packages"):
        names.extend(("skill_files", "skill_read"))
    live = await registered_helpers(repo, [name for name in names if name in pins])
    return {name: r for name, r in live.items() if r.digest == pins[name]}


def descriptor(revision):
    return {
        "name": revision.name,
        "description": revision.contract.description,
        "inputSchema": revision.contract.input_schema,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    }


def check_output(revision, result):
    if not schema_validator(revision.contract.output_schema).is_valid(result):
        raise CapabilityError(
            "Session tool result differs from the registered contract"
        )
