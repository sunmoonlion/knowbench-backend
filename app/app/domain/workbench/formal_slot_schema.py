"""Compose a method interface without changing the scope of slot-local references."""

from copy import deepcopy


def slot_object_schema(slots):
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
            for name, slot in slots.items()
        },
        "required": [
            name
            for name, slot in slots.items()
            if slot.required or slot.on_missing != "omit"
        ],
        "additionalProperties": False,
    }
