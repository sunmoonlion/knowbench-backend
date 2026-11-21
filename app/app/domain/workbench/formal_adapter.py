"""Resolve the identity of a frozen direct implementation and its reviewed policy."""

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.tool_registry import (
    ToolRegistration,
    connection_registration,
    connection_source,
)

# These are the runner's direct formal-method adapters. Platform definitions
# require a reviewed host adapter before they can be dispatched as a method.
DIRECT_HOST_ADAPTERS = {
    "mcp": "formal_mcp",
    "skill_script": "formal_script",
    "skill_instruction": "formal_instruction",
}


def host_kind(source, *, instruction_leaf=False):
    adapter = DIRECT_HOST_ADAPTERS.get(source.kind)
    if adapter is None or (adapter == "formal_instruction" and not instruction_leaf):
        raise CapabilityError("No formal host adapter for this implementation")
    return adapter


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
        if connection_source(asset) == "mcp":
            registration = connection_registration(asset, source.entry)
            if source.digest != "sha256:" + registration.descriptor_digest:
                raise CapabilityError("MCP interface differs from its frozen source")
            return registration
    raise CapabilityError("The registered formal implementation is unavailable")
