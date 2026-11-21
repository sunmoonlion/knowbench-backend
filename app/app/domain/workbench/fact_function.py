"""Project the registered provider interface into the host's formal argument contract."""

from copy import deepcopy

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.tool_registry import ToolRegistration

# Logical permission destination; transport resolves only the configured internal KnowBase.
DESTINATION = "https://knowbase/api/internal/v1/functions/query_facts/1"


def require_fact_source(source):
    if (source.kind, source.source, source.entry, source.version) != (
        "service_function",
        "knowbase",
        "query_facts",
        "1",
    ):
        raise CapabilityError("Unsupported service function revision")


def descriptor(revision):
    if (
        revision.contract.operation != "query_facts"
        or revision.contract.required_permissions != ["facts.read"]
    ):
        raise CapabilityError(
            "Function registration differs from the supported operation"
        )
    schema = deepcopy(revision.contract.input_schema)
    # Only the host may translate the approved scope into the provider's actual grant.
    if (
        not isinstance(schema.get("properties"), dict)
        or "scope" not in schema["properties"]
    ):
        raise CapabilityError("Fact query requires a registered scope argument")
    for name in ("run", "session"):
        schema["properties"].pop(name, None)
    schema["additionalProperties"] = False
    schema["required"] = sorted(set(schema.get("required", [])) | {"scope"})
    return {
        "name": "query_facts",
        "description": revision.contract.description,
        "inputSchema": schema,
    }


def registration(source, tool):
    require_fact_source(source)
    if tool.get("name") != source.entry:
        raise CapabilityError("Function name differs from the pinned source")
    return ToolRegistration.discover("service_function", DESTINATION, tool)
