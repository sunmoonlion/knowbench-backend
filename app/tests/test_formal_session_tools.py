"""Formal selection identity and composite tool schemas, without model calls."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.application.workbench.formal_session_tools import (
    formal_tool_key,
    method_input_schema,
)
from app.domain.workbench.capabilities import SelectionPut
from app.domain.workbench.formal_method_generated import MethodRef, ValueSlot


def ref(**update):
    return MethodRef.model_validate(
        {
            "method_id": str(uuid4()),
            "version": 1,
            "digest": "sha256:" + "a" * 64,
            **update,
        }
    )


def test_formal_selection_is_empty_by_default_and_one_version_per_method():
    assert SelectionPut(revision=0).formal_methods == ()
    first = ref()
    for second in (first, first.model_copy(update={"version": 2})):
        with pytest.raises(ValidationError):
            SelectionPut(revision=1, formal_methods=(first, second))
    with pytest.raises(ValidationError):
        SelectionPut(revision=1, formal_methods=tuple(ref() for _ in range(21)))
    assert formal_tool_key(first) == formal_tool_key(
        MethodRef.model_validate(first.model_dump(mode="json"))
    )
    assert formal_tool_key(first) != formal_tool_key(
        first.model_copy(update={"version": 2})
    )


def test_nested_slot_local_references_keep_their_meaning_when_exposed_as_object():
    slot = ValueSlot.model_validate(
        {
            "description": "A short value",
            "schema": {
                "$defs": {"text": {"type": "string", "minLength": 2}},
                "$ref": "#/$defs/text",
            },
            "required": True,
            "meaning": {},
            "on_missing": "fail",
        }
    )
    schema = method_input_schema(SimpleNamespace(inputs={"message": slot}))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"message": "ok"})
    assert not validator.is_valid({"message": "x"})
    assert not validator.is_valid({"message": "ok", "undeclared": True})
    assert not validator.is_valid({})
    assert slot.schema_["$ref"] == "#/$defs/text"


def test_schema_reference_relocation_does_not_rewrite_literal_values():
    literal = {"$ref": "#/$defs/a"}
    slot = ValueSlot.model_validate(
        {
            "description": "Literal object",
            "schema": {
                "$defs": {"a": {}},
                "const": literal,
                "default": literal,
                "examples": [literal],
            },
            "required": True,
            "meaning": {},
            "on_missing": "fail",
        }
    )
    schema = method_input_schema(SimpleNamespace(inputs={"value": slot}))
    assert Draft202012Validator(schema).is_valid({"value": literal})
    assert schema["properties"]["value"] == slot.schema_
