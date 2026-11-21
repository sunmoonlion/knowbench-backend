"""Project the registered provider interface into the host's formal argument contract."""

from copy import deepcopy

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.tool_registry import ToolRegistration

# Logical permission destination; transport resolves only the configured internal KnowBase.
QUERY_PERMISSIONS = {
    "query_facts": "facts.read",
    "query_metric": "facts.read",
    "read_facts": "facts.read",
    "list_inputs": "inputs.list",
    "read_input": "inputs.read",
    "lookup": "materials.lookup",
    "describe_items": "vocabulary.read",
    "search_evidence": "evidence.search",
    "list_library": "library.list",
    "read_method_input": "method.inputs.read",
}
DATA_QUERIES = frozenset(QUERY_PERMISSIONS)


def destination(name):
    if name not in DATA_QUERIES:
        raise CapabilityError("Unsupported KnowBase query")
    return f"https://knowbase/api/internal/v1/functions/{name}/1"


def require_data_source(source):
    if (
        source.entry in {"read_input", "lookup", "describe_items", "list_library"}
        or source.entry not in DATA_QUERIES
        or (
            source.kind,
            source.source,
            source.version,
        )
        != (
            "service_function",
            "knowbase",
            "1",
        )
    ):
        raise CapabilityError("Unsupported service function revision")


def descriptor(revision):
    if (
        (revision.provider, revision.version) != ("knowbase", 1)
        or revision.name not in DATA_QUERIES
        or revision.contract.operation != revision.name
        or revision.contract.required_permissions != [QUERY_PERMISSIONS[revision.name]]
    ):
        raise CapabilityError(
            "Function registration differs from the supported operation"
        )
    schema = deepcopy(revision.contract.input_schema)
    # Only the host may translate the approved scope into the provider's actual grant.
    if not isinstance(schema.get("properties"), dict) or (
        revision.name != "read_method_input" and "scope" not in schema["properties"]
    ):
        raise CapabilityError("Data query requires a registered scope argument")
    for name in ("run", "session"):
        schema["properties"].pop(name, None)
    schema["additionalProperties"] = False
    if revision.name != "read_method_input":
        schema["required"] = sorted(set(schema.get("required", [])) | {"scope"})
    return {
        "name": revision.name,
        "description": revision.contract.description,
        "inputSchema": schema,
    }


def registration(source, tool):
    require_data_source(source)
    if tool.get("name") != source.entry:
        raise CapabilityError("Function name differs from the pinned source")
    return ToolRegistration.discover(
        "service_function", destination(source.entry), tool
    )
