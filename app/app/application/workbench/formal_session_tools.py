"""Explicit conversation selections of admitted model-callable private methods.

Selection and exposure confer no operation grant. The common call service checks
this frozen spec again before requesting and consuming each owner's approval.
"""

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import Forbidden, WorkbenchError
from app.domain.workbench.formal_execution import direct_procedure
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.formal_slot_schema import slot_object_schema
from app.domain.workbench.tool_registry import formal_tool_key as formal_tool_key


class FormalSessionTools:
    def __init__(self, capabilities):
        self.capabilities, self.repo = capabilities, capabilities.repo
        self.catalog = FormalMethodCatalog(
            self.repo, capabilities.formal_sources, capabilities.operations
        )

    async def resolve(self, owner, reference, *, eligible=None):
        reference = MethodRef.model_validate(reference)
        if eligible is None:
            await self.capabilities.require_formal_use(
                owner, reference.model_dump(mode="json")
            )
        elif reference not in eligible:
            raise Forbidden("This method version is not listed for the owner")
        resolved = await self.catalog.resolve_owned(owner, reference, caller="model")
        frozen, asset, policy = resolved.frozen, resolved.asset, resolved.policy
        procedure = direct_procedure(frozen.draft, caller="model")
        source = frozen.draft.source
        if source is None:
            raise CapabilityError("Formal method has no pinned source")
        if resolved.registration.source != "mcp":
            raise CapabilityError("Formal method source tool is unavailable")
        implementation_tool = resolved.registration.descriptor()
        declaration = frozen.draft.definition.capability_declaration
        return {
            "execution_kind": "formal_method_model",
            "tool": {
                "name": formal_tool_key(reference),
                "description": declaration.title
                + ": "
                + declaration.purpose
                + ". Invoke this exact registered method; each operation requires the owner's approval. Only the verified typed method result indicates success.",
                "inputSchema": slot_object_schema(procedure.inputs),
            },
            "implementation_tool": implementation_tool,
            "source_asset_id": asset["id"],
            "connection_id": asset["id"],
            "revision": asset["revision"],
            "service_url": asset["detail"]["url"],
            "operation_digest": policy.policy_digest,
            "access": {
                "requirement": frozen.draft.definition.access_requirements[
                    0
                ].model_dump(mode="json"),
                "operation_digest": policy.policy_digest,
            },
            "formal_method": {
                "reference": reference.model_dump(mode="json"),
                "frozen": frozen.model_dump(mode="json", by_alias=True),
                "registration_digest": resolved.registration.descriptor_digest,
                "source_digest": source.fingerprint(),
            },
        }

    async def issues(self, owner, references, *, eligible=None):
        issues = []
        if eligible is None:
            eligible = await self.capabilities.eligible_formal_refs(owner, references)
        for raw in references:
            reference = MethodRef.model_validate(raw)
            try:
                await self.resolve(owner, reference, eligible=eligible)
            except (WorkbenchError, ValueError):
                issues.append(
                    {
                        "ref": formal_tool_key(reference),
                        "code": "formal_method_unavailable",
                    }
                )
        return issues

    async def available(self, owner, runtime):
        """Current rights must still match the prepared snapshot at discovery."""
        tools = self.capabilities.runtime_tools(runtime)
        for key, spec in list(tools.items()):
            if spec.get("execution_kind") != "formal_method_model":
                continue
            try:
                fresh = await self.resolve(owner, spec["formal_method"]["reference"])
                if fresh != spec or key != fresh["tool"]["name"]:
                    tools.pop(key)
            except (WorkbenchError, ValueError, KeyError):
                tools.pop(key)
        return tools
