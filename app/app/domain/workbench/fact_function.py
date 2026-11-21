"""Project the registered provider interface into the host's formal argument contract."""

from copy import deepcopy

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.tool_registry import ToolRegistration

# Logical permission destination; transport resolves only the configured internal KnowBase.
FACT_QUERIES = frozenset({"query_facts", "query_metric", "read_facts"})


def destination(name):
    if name not in FACT_QUERIES:
        raise CapabilityError("Unsupported KnowBase query")
    return f"https://knowbase/api/internal/v1/functions/{name}/1"


def require_fact_source(source):
    if source.entry not in FACT_QUERIES or (
        source.kind,
        source.source,
        source.version,
    ) != (
        "service_function",
        "knowbase",
        "1",
    ):
        raise CapabilityError("Unsupported service function revision")


def descriptor(revision):
    if (
        (revision.provider, revision.version) != ("knowbase", 1)
        or revision.name not in FACT_QUERIES
        or revision.contract.operation != revision.name
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
        "name": revision.name,
        "description": revision.contract.description,
        "inputSchema": schema,
    }


def registration(source, tool):
    require_fact_source(source)
    if tool.get("name") != source.entry:
        raise CapabilityError("Function name differs from the pinned source")
    return ToolRegistration.discover(
        "service_function", destination(source.entry), tool
    )
