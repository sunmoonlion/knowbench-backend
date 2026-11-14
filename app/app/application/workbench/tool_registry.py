"""API registration projections for platform definitions and user discovery snapshots."""

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.platform_tools import TOOLS
from app.domain.workbench.skill_execution import script_tool
from app.domain.workbench.tool_registry import ToolRegistration, ToolRegistry

PLATFORM_TOOLS = {
    name: ToolRegistration.discover("platform", "knowbench", {"name": name, **spec})
    for name, spec in TOOLS.items()
}


def platform_catalog(data_tools: list[dict]) -> ToolRegistry:
    return ToolRegistry(
        [
            *PLATFORM_TOOLS.values(),
            *(
                ToolRegistration.discover("knowbase", "knowbase-data", tool)
                for tool in data_tools
            ),
        ]
    )


def mcp_catalog(url: str, tools: list[dict]) -> ToolRegistry:
    """Generic discovery cannot assert a verified business operation or read scope."""
    return ToolRegistry(ToolRegistration.discover("mcp", url, tool) for tool in tools)


def registered_call(asset: dict, spec: dict) -> ToolRegistration:
    """Resolve against the current account's asset, then compare the frozen definition."""
    if spec.get("execution_kind") == "skill_script":
        tool = script_tool(asset) if asset["kind"] == "skill" else None
        if tool is None:
            raise CapabilityError("Skill has no registered script entrypoint")
        current = ToolRegistration.discover(
            "skill_script", asset["detail"]["digest"], tool
        )
    else:
        if asset["kind"] != "connection":
            raise CapabilityError("Tool requires a connection")
        requested = ToolRegistration.discover(
            "mcp", asset["detail"]["url"], spec["tool"]
        )
        # Installation validates the full snapshot; a call only needs its selected
        # definition. Do not recompile every installed schema for every invocation.
        catalog = mcp_catalog(
            asset["detail"]["url"],
            [t for t in asset["detail"]["tools"] if t["name"] == requested.name],
        )
        current = catalog.get(requested.tool_id)
    frozen = ToolRegistration.discover(current.source, current.service, spec["tool"])
    if current.descriptor_digest != frozen.descriptor_digest:
        raise CapabilityError("Registered tool definition changed; prepare a new turn")
    return current
