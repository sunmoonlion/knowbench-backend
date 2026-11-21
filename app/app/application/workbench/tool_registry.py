"""API registration projections for platform definitions and user discovery snapshots."""

from app.domain.workbench.capabilities import CapabilityError, tool_key
from app.domain.workbench.errors import NotFound
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.platform_tools import TOOLS
from app.domain.workbench.registry_views import (
    FormalRegistration,
    InstallationEntry,
    InstalledRegistration,
)
from app.domain.workbench.skill_execution import script_tool
from app.domain.workbench.tool_registry import (
    ToolRegistration,
    ToolRegistry,
    connection_registration,
    connection_source,
)


async def installed_registration(
    repo, owner: str, asset_id: str
) -> InstalledRegistration:
    asset = await repo.capability_asset(owner, asset_id)
    detail = asset["detail"]
    entries = []
    if asset["kind"] == "connection":
        protocol = connection_source(asset)
        catalog = mcp_catalog(detail["url"], detail.get("tools", []), protocol=protocol)
        entries = [
            InstallationEntry(
                kind=protocol, entry=tool.name, tool_key=tool_key(asset_id, tool.name)
            )
            for tool in catalog.registrations()
        ]
    elif asset["kind"] == "skill":
        entries.append(InstallationEntry(kind="skill_instruction", entry="SKILL.md"))
        if tool := script_tool(asset):
            entries.append(
                InstallationEntry(
                    kind="skill_script",
                    entry=tool["name"],
                    tool_key=tool_key(asset_id, tool["name"]),
                )
            )
    return InstalledRegistration(
        asset_id=asset["id"],
        revision=asset["revision"],
        status=asset["status"],
        entries=entries,
    )


async def formal_registration(
    repo, owner: str, reference: MethodRef
) -> FormalRegistration:
    row = await repo.formal_method(owner, str(reference.method_id), reference.version)
    if not row or row["digest"] != reference.digest:
        raise NotFound("Formal method registration not found")
    frozen = FrozenFormalMethod.model_validate(row["document"])
    if frozen.reference != reference:
        raise CapabilityError("Registered method differs from the requested version")
    source = frozen.draft.source
    return FormalRegistration(
        reference=reference,
        title=frozen.draft.definition.capability_declaration.title,
        source_asset_id=source.asset_id if source else None,
        source_revision=source.asset_revision if source else None,
        source_entry=source.entry if source else None,
        source_kind=source.kind if source else None,
        source_digest=source.digest if source else None,
        dependencies=frozen.validation.dependencies,
    )


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


def mcp_catalog(url: str, tools: list[dict], *, protocol="mcp") -> ToolRegistry:
    """Generic discovery cannot assert a verified business operation or read scope."""
    if protocol not in ("mcp", "a2a"):
        raise CapabilityError("Unsupported registered connection protocol")
    return ToolRegistry.snapshot(protocol, url, tools)


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
        current = connection_registration(asset, spec["tool"].get("name"))
    frozen = ToolRegistration.discover(current.source, current.service, spec["tool"])
    if current.descriptor_digest != frozen.descriptor_digest:
        raise CapabilityError("Registered tool definition changed; prepare a new turn")
    return current
