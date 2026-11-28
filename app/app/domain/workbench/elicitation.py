"""Present primitive MCP form fields through the workbench question contract."""

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


def form_questions(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return []
    if set(schema) - {
        "type",
        "properties",
        "required",
        "title",
        "description",
        "additionalProperties",
        "$schema",
    }:
        return []
    fields = schema.get("properties")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 32:
        return []
    questions = []
    for name, field in fields.items():
        if not isinstance(field, dict) or field.get("type") not in (
            "string",
            "boolean",
            "integer",
            "number",
        ):
            return []
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or field.get("format") == "password"
            or field.get("writeOnly")
        ):
            return []
        if set(field) - {
            "type",
            "enum",
            "oneOf",
            "title",
            "description",
            "default",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "format",
        }:
            return []
        choices = field.get("enum", [])
        if "oneOf" in field:
            variants = field["oneOf"]
            if not isinstance(variants, list) or any(
                not isinstance(v, dict)
                or "const" not in v
                or set(v) - {"const", "title", "description"}
                for v in variants
            ):
                return []
            choices = [v["const"] for v in variants]
        if not isinstance(choices, list) or any(
            isinstance(v, (list, dict)) for v in choices
        ):
            return []
        if field["type"] == "boolean" and not choices:
            choices = [True, False]
        options = [
            {
                "label": value if isinstance(value, str) else json.dumps(value),
                "description": "",
            }
            for value in choices
        ]
        questions.append(
            {
                "id": name,
                "header": field.get("title") or name,
                "question": field.get("description") or field.get("title") or name,
                "options": options,
                "isOther": not bool(options),
            }
        )
    if schema.get("additionalProperties") not in (None, False, True):
        return []
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return []
    return questions


def form_reply(schema, answers):
    declined = {"action": "decline"}
    if not answers:
        return declined
    values = {}
    for name, entry in answers.get("answers", {}).items():
        choices = entry.get("answers") or []
        if not choices:
            continue
        if len(choices) != 1 or name not in schema["properties"]:
            return declined
        value = choices[0]
        if schema["properties"][name]["type"] != "string":
            try:
                value = json.loads(value, parse_constant=_invalid_constant)
            except (TypeError, ValueError, RecursionError):
                return declined
        values[name] = value
    if not values or not Draft202012Validator(schema).is_valid(values):
        return declined
    return {"action": "accept", "content": values}


def _invalid_constant(value):
    raise ValueError("non-finite form value")
