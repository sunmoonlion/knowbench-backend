"""Optional checks remain fail-closed and do not rewrite historical snapshots."""

import json

import pytest
from pydantic import ValidationError

from app.application.workbench.acceptance import judge
from app.application.workbench.acceptance_numbers import _computed
from app.domain.workbench.packs import AcceptanceRule


def test_legacy_serialization_omits_extensions():
    rule = AcceptanceRule(kind="computed", path="result")
    for key in ("expected_value", "input_keys", "non_empty_keys", "unique_by"):
        assert key not in rule.model_dump(mode="json")
    assert AcceptanceRule.model_validate(rule.model_dump()) == rule


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "each_has", "expected_value": 0},
        {"kind": "computed", "expected_value": True},
        {"kind": "computed", "expected_value": float("nan")},
        {"kind": "computed", "expected_value": float("inf")},
        {"kind": "computed", "non_empty_keys": ["x"]},
        {"kind": "each_has", "input_keys": ["x"]},
        {"kind": "each_has", "unique_by": [" "]},
        {"kind": "each_has", "unique_by": ["x", "x"]},
    ],
)
def test_invalid_extensions_are_rejected(fields):
    with pytest.raises(ValidationError):
        AcceptanceRule.model_validate({"path": "rows", **fields})


def test_expected_value_checks_the_real_computation_not_just_a_matching_claim():
    rule = AcceptanceRule(kind="computed", path="result", expected_value=0, tolerance=1)
    for value, accepted in [
        (0, True),
        (1, True),
        (-1, True),
        (2, False),
        (999999, False),
    ]:
        errors = _computed(
            rule, [{"compute_id": "c1", "value": value}], {"c1": {"value": str(value)}}
        )
        assert (not errors) == accepted


def test_input_fields_must_match_recorded_operands():
    rule = AcceptanceRule(kind="computed", path="rows", input_keys=("lower", "upper"))
    item = {"compute_id": "c1", "value": 0, "lower": 1, "upper": 3}
    record = {
        "c1": {
            "value": "0",
            "inputs": [
                {"name": "lower", "value": "1"},
                {"name": "upper", "value": "3"},
            ],
        }
    }
    assert not _computed(rule, [item], record)
    for mutation in ({"lower": 4}, {"upper": None}, {"lower": True}):
        assert _computed(rule, [item | mutation], record)
    assert _computed(rule, [item], {"c1": {"value": "0"}})


@pytest.mark.parametrize(
    "rows, accepted",
    [
        ([{"period": "Q1", "metric": "a", "value": 0}], True),
        ([{"period": "Q1", "metric": "a", "value": False}], True),
        ([{"period": "Q1", "metric": " "}], False),
        ([{"period": "Q1", "metric": "a"}] * 2, False),
        ([{"period": "Q1", "metric": "a"}, {"period": "Q2", "metric": "a"}], True),
        ([{"period": [], "metric": "a"}], False),
    ],
)
def test_row_constraints(rows, accepted):
    rule = AcceptanceRule(
        kind="each_has",
        path="rows",
        non_empty_keys=("period", "metric"),
        unique_by=("period", "metric"),
    )
    assert judge(json.dumps({"rows": rows}), (rule,)).ok == accepted


def test_unique_numeric_identity_does_not_distinguish_integer_and_float():
    rule = AcceptanceRule(kind="each_has", path="rows", unique_by=("id",))
    assert not judge('{"rows":[{"id":1},{"id":1.0}]}', (rule,)).ok
    assert not judge('{"rows":[{"id":NaN}]}', (rule,)).ok
    assert judge('{"rows":[{"id":1},{"id":true}]}', (rule,)).ok


def test_expected_value_does_not_double_the_tolerance_for_reported_result():
    rule = AcceptanceRule(kind="computed", path="result", expected_value=0, tolerance=1)
    assert _computed(rule, [{"compute_id": "c1", "value": 2}], {"c1": {"value": "1"}})
