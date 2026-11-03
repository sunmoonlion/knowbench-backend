"""Acceptance must compare reported decimals, not float-rounded substitutes."""

import json

import pytest

from app.application.workbench.acceptance import judge
from app.application.workbench.acceptance_numbers import _computed
from app.domain.workbench.packs import AcceptanceRule

VALUE = "0.9154029126035511584400534399"


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "Result:\n{}\nDone."])
def test_exact_computation_survives_decoding_and_json_storage(wrapper):
    rule = AcceptanceRule(kind="computed", path="calculations")
    raw = '{"calculations":[{"value":' + VALUE + ',"compute_id":"c873"}]}'
    verdict = judge(wrapper.format(raw), (rule,))
    assert verdict.ok
    stored = json.loads(json.dumps(verdict.parsed))
    assert stored["calculations"][0]["value"] == VALUE
    assert not _computed(rule, stored["calculations"], {"c873": {"value": VALUE}})


@pytest.mark.parametrize(
    "reported", ["0.9154029126035511", "0.9154029126035511584400534398"]
)
def test_truncation_or_wrong_last_decimal_is_still_rejected(reported):
    rule = AcceptanceRule(kind="computed", path="rows")
    raw = '{"rows":[{"value":' + reported + ',"compute_id":"c873"}]}'
    rows = judge(raw, (rule,)).parsed["rows"]
    assert _computed(rule, rows, {"c873": {"value": VALUE}})


def test_record_ownership_expression_and_operand_checks_remain_strict():
    rule = AcceptanceRule(
        kind="computed", path="row", input_keys=("x",), expression="x"
    )
    raw = '{"row":{"value":' + VALUE + ',"x":' + VALUE + ',"compute_id":"c1"}}'
    row = judge(raw, (rule,)).parsed["row"]
    record = {
        "value": VALUE,
        "expression": "x",
        "inputs": [{"name": "x", "value": VALUE}],
    }
    assert not _computed(rule, [row], {"c1": record})
    assert _computed(rule, [row], {})
    assert _computed(rule, [row], {"c1": record | {"expression": "x+1"}})
    assert _computed(rule, [row | {"x": "0.9"}], {"c1": record})


def test_sourced_values_preserved_without_changing_ordinary_fields():
    rule = AcceptanceRule(kind="sourced", path="nested.rows")
    raw = '{"nested":{"rows":[{"value":1.00000000000000000001,"page":3}]},"horizon":2,"normal":0.25}'
    parsed = judge(raw, (rule,)).parsed
    assert parsed["nested"]["rows"][0] == {"value": "1.00000000000000000001", "page": 3}
    assert parsed["horizon"] == 2
    assert isinstance(parsed["normal"], float)


@pytest.mark.parametrize(
    "value", ["0", "1.25", '"1.250"', "true", "null", "NaN", "Infinity"]
)
def test_existing_value_types_and_invalid_values_are_not_reinterpreted(value):
    rule = AcceptanceRule(kind="computed", path="row")
    raw = '{"row":{"value":' + value + ',"compute_id":"c1"}}'
    parsed = judge(raw, (rule,)).parsed
    assert json.dumps(parsed) == json.dumps(json.loads(raw))


@pytest.mark.parametrize("rows", ["[]", "[null]", "null", "1", '"invalid"'])
def test_malformed_rows_remain_for_normal_validation(rows):
    rule = AcceptanceRule(kind="computed", path="rows")
    assert judge('{"rows":' + rows + "}", (rule,)).parsed == json.loads(
        '{"rows":' + rows + "}"
    )
