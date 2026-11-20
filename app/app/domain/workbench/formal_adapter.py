"""Resolve the identity of a frozen direct implementation and its reviewed policy."""

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.tool_registry import ToolRegistration


def host_kind(source):
    if source.kind == "mcp":
        return "formal_mcp"
    if source.kind == "skill_script":
        return "formal_script"
    raise CapabilityError("No formal host adapter for this implementation")


def script_registration(draft):
    source = draft.source
    procedure = draft.definition.procedure
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {k: v.schema_ for k, v in procedure.inputs.items()},
        "required": [
            k
            for k, v in procedure.inputs.items()
            if v.required or v.on_missing != "omit"
        ],
    }
    return ToolRegistration.discover(
        "skill_script",
        source.digest,
        {
            "name": "skill_run",
            "description": f"Execute the installed entry {source.entry}",
            "inputSchema": schema,
        },
    )


def retained_registration(draft, tool):
    source = draft.source
    if host_kind(source) == "formal_script":
        registration = script_registration(draft)
        if tool != registration.descriptor():
            raise CapabilityError("Script interface or frozen entry changed")
        return registration
    registration = ToolRegistration.discover("mcp", source.source, tool)
    if (
        registration.name != source.entry
        or source.digest != "sha256:" + registration.descriptor_digest
    ):
        raise CapabilityError("MCP interface differs from its frozen source")
    return registration


def live_registration(draft, asset):
    source = draft.source
    if source.kind == "skill_script" and asset["kind"] == "skill":
        # The licensed manifest and exact asset revision are verified by FormalMethods._source.
        return script_registration(draft)
    if (
        source.kind == "mcp"
        and asset["kind"] == "connection"
        and asset["detail"]["url"] == source.source
    ):
        for tool in asset["detail"].get("tools", []):
            if tool["name"] == source.entry:
                return retained_registration(draft, tool)
    raise CapabilityError("The registered formal implementation is unavailable")
