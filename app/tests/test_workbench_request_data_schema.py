"""Model-facing requests must reject invalid values observed in release #44."""

import copy

import pytest
from jsonschema import Draft202012Validator

from app.application.workbench.expert_data import _asked_need
from app.domain.workbench.platform_tools import TOOLS

SCHEMA = TOOLS["request_data"]["inputSchema"]
VALIDATOR = Draft202012Validator(SCHEMA)
NEED = {
    "subject": "600519.SH",
    "doc_type": "annual_report",
    "period": {"kind": "fixed", "start": "2021-01-01", "end": "2024-12-31"},
    "reading": "table",
    "essential": True,
    "reason": "用户指定期间的财务数据",
}


@pytest.mark.parametrize(
    "kind", ["annual_report", "q1_report", "semiannual_report", "q3_report"]
)
def test_periodic_reports_keep_explicit_years(kind):
    need = {**NEED, "doc_type": kind}
    VALIDATOR.validate({"task": "test-task", "needs": [need]})
    parsed = _asked_need(need, 1, 1)
    assert parsed.doc_type == kind
    assert parsed.period.kind == "fixed"
    assert str(parsed.period.start) == "2021-01-01"
    assert str(parsed.period.end) == "2024-12-31"


@pytest.mark.parametrize(
    "field,value",
    [
        ("doc_type", "first_quarter_report"),
        ("doc_type", "third_quarter_report"),
        ("doc_type", "interim_report"),
        ("doc_type", "semi_annual_report"),
        ("topics", ["第一季度报告"]),
        ("period", {"kind": "recent", "months": 66}),
        ("period", {"kind": "latest", "count": 11}),
        ("period", {"kind": "unknown"}),
    ],
)
def test_schema_exposes_contract_rejections_before_tool_execution(field, value):
    need = {**NEED, field: value}
    assert list(VALIDATOR.iter_errors({"task": "test-task", "needs": [need]}))


def test_schema_is_self_contained_and_does_not_restrict_nonfinancial_data():
    Draft202012Validator.check_schema(SCHEMA)
    need = copy.deepcopy(NEED)
    need.pop("subject")
    need.update(doc_type="other_text", period={"kind": "none"}, reading="text")
    VALIDATOR.validate({"task": "test-task", "needs": [need]})
    assert _asked_need(need, 1, 1).subject is None
