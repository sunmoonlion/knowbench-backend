"""Explicit conversation selections of admitted model-callable private methods.

Selection and exposure confer no operation grant. The common call service checks
this frozen spec again before requesting and consuming each owner's approval.
"""

from copy import deepcopy

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.formal_execution import direct_procedure
from app.domain.workbench.formal_method_generated import MethodRef


def formal_tool_key(reference: MethodRef) -> str:
    return "formal_method_" + digest(reference.model_dump(mode="json"))[:32]


def method_input_schema(procedure):
    def relocate(value, prefix):
        if not isinstance(value, dict):
            return deepcopy(value)
        schemas = {
            "properties",
            "patternProperties",
            "$defs",
            "definitions",
            "dependentSchemas",
        }
        sequence = {"allOf", "anyOf", "oneOf", "prefixItems"}
        single = {
            "items",
            "contains",
            "not",
            "if",
            "then",
            "else",
            "additionalProperties",
            "unevaluatedProperties",
            "propertyNames",
            "unevaluatedItems",
            "additionalItems",
        }
        result = deepcopy(value)
        for key, item in value.items():
            if key == "$ref":
                result[key] = prefix + item[1:]
            elif key in schemas:
                result[key] = {
                    name: relocate(schema, prefix) for name, schema in item.items()
                }
            elif key in sequence:
                result[key] = [relocate(schema, prefix) for schema in item]
            elif key in single:
                result[key] = relocate(item, prefix)
            elif key == "dependencies":
                result[key] = {
                    name: relocate(schema, prefix)
                    if isinstance(schema, dict)
                    else deepcopy(schema)
                    for name, schema in item.items()
                }
        return result

    return {
        "type": "object",
        "properties": {
            name: relocate(
                slot.schema_,
                "#/properties/" + name.replace("~", "~0").replace("/", "~1"),
            )
            for name, slot in procedure.inputs.items()
        },
        "required": [
            name
            for name, slot in procedure.inputs.items()
            if slot.required or slot.on_missing != "omit"
        ],
        "additionalProperties": False,
    }


class FormalSessionTools:
    def __init__(self, capabilities):
        self.capabilities, self.repo = capabilities, capabilities.repo
        self.catalog = FormalMethodCatalog(
            self.repo, capabilities.formal_sources, capabilities.operations
        )

    async def resolve(self, owner, reference):
        reference = MethodRef.model_validate(reference)
        await self.capabilities.require_formal_use(
            owner, reference.model_dump(mode="json")
        )
        resolved = await self.catalog.resolve_owned(owner, reference, caller="model")
        frozen, asset, policy = resolved.frozen, resolved.asset, resolved.policy
        procedure = direct_procedure(frozen.draft, caller="model")
        source = frozen.draft.source
        if source is None:
            raise CapabilityError("Formal method has no pinned source")
        implementation_tool = next(
            (
                tool
                for tool in asset["detail"].get("tools", [])
                if tool["name"] == source.entry
            ),
            None,
        )
        if implementation_tool is None:
            raise CapabilityError("Formal method source tool is unavailable")
        declaration = frozen.draft.definition.capability_declaration
        return {
            "execution_kind": "formal_method_model",
            "tool": {
                "name": formal_tool_key(reference),
                "description": declaration.title
                + ": "
                + declaration.purpose
                + ". Invoke this exact registered method; each operation requires the owner's approval. Only the verified typed method result indicates success.",
                "inputSchema": method_input_schema(procedure),
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

    async def issues(self, owner, references):
        issues = []
        for raw in references:
            reference = MethodRef.model_validate(raw)
            try:
                await self.resolve(owner, reference)
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
