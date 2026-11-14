"""Bounded tool schema validation with document-local references only."""

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

from app.domain.workbench.capabilities import CapabilityError


def schema_validator(schema):
    # Never allow a JSON Schema resolver to fetch arbitrary network/file resources.
    pending, count = [(schema, 0)], 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > 5000 or depth > 32:
            raise CapabilityError("Tool schema exceeds the complexity limit")
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$id":
                    raise CapabilityError(
                        "Tool schemas cannot change their resolution base"
                    )
                if key in ("$ref", "$dynamicRef") and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise CapabilityError(
                        "Only document-local schema references are supported"
                    )
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError) as exc:
        raise CapabilityError("Invalid tool input schema") from exc
    return Draft202012Validator(schema, registry=Registry())
