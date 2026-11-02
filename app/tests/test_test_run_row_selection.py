"""用例按业务键定位表行；不依赖顺序，不将缺行或重复行放行。"""

import pytest
from pydantic import ValidationError

from app.domain.workbench.test_runs import Expectation, expectation_met

BASE = {
    "artifact": "metrics",
    "path": "annual",
    "kind": "approx",
    "value": 100,
    "tolerance": 0.001,
}
ROW = {"where": {"year": "2024"}, "field": "revenue"}


@pytest.mark.parametrize(
    "rows,passed",
    [
        ([{"year": "2024", "revenue": 100}], True),
        ([{"year": "2023", "revenue": 1}, {"year": "2024", "revenue": 100}], True),
        ([{"year": "2024", "revenue": 100}, {"year": "2023", "revenue": 1}], True),
        ([{"year": "2024", "revenue": 100.1}], True),
        ([{"year": "2024", "revenue": 100.11}], False),
        ([{"year": "2024", "revenue": 100}] * 2, False),
        ([{"year": "2024", "revenue": 100}, {"year": "2024", "revenue": 0}], False),
        ([{"year": "2023", "revenue": 100}], False),
        ([{"year": 2024, "revenue": 100}], False),
        ([{"year": "2024", "revenue": None}], False),
        ([{"year": "2024"}], False),
        ([None, 100, "2024", {"revenue": 100}], False),
        ([], False),
        ({"year": "2024", "revenue": 100}, False),
    ],
)
def test_unique_row_selected_before_comparison(rows, passed):
    expected = Expectation.model_validate(BASE | {"row": ROW})
    assert expectation_met(expected, {"metrics": {"annual": rows}}) is passed


def test_compound_keys_nested_values_and_strict_boolean_matching():
    expected = Expectation.model_validate(
        BASE
        | {"row": {"where": {"year": 2024, "group": True}, "field": "numbers.revenue"}}
    )
    rows = [
        {"year": 2024, "group": 1, "numbers": {"revenue": 0}},
        {"year": 2024, "group": True, "numbers": {"revenue": 100}},
        {"year": 2024, "group": False, "numbers": {"revenue": 0}},
    ]
    assert expectation_met(expected, {"metrics": {"annual": rows}})
    assert not expectation_met(expected, {"metrics": {"annual": rows[:1]}})


def test_legacy_serialization_and_positional_paths_are_unchanged():
    expected = Expectation.model_validate(BASE | {"path": "annual.0.revenue"})
    assert "row" not in expected.model_dump(mode="json")
    assert expectation_met(expected, {"metrics": {"annual": [{"revenue": 100}]}})
    assert (
        Expectation.model_validate(BASE | {"row": None}).model_dump()
        == Expectation.model_validate(BASE).model_dump()
    )


@pytest.mark.parametrize(
    "row",
    [
        {"where": {}, "field": "value"},
        {"where": {" ": "2024"}, "field": "value"},
        {"where": {"x": []}, "field": "value"},
        {"where": {"x": float("inf")}, "field": "value"},
        {"where": {"x": float("nan")}, "field": "value"},
        {"where": {"x": "y"}, "field": " "},
        {"where": {str(n): n for n in range(9)}, "field": "value"},
        {"where": {"x": "y"}, "field": "value", "fallback_index": 0},
    ],
)
def test_invalid_selectors_cannot_enter_a_run(row):
    with pytest.raises(ValidationError):
        Expectation.model_validate(BASE | {"row": row})
