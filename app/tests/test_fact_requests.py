"""第 0 步「要哪些数」（SDD 0051）：模型按指标提，不写渠道；存下来读回、用户改过都仍是这一种。"""

import pytest

from app.application.workbench.expert_data import (
    _asked_need,
    _revised,
    request_needs,
)
from app.domain.workbench.data_plan import _label
from app.domain.workbench.data_request import DataRequest, FactRequest
from app.domain.workbench.errors import DataChoiceInvalid

ASKED = {
    "subject": "300750.SZ",
    "metrics": ["IS.REVENUE", "IS.NET_PROFIT_PARENT"],
    "period": {"kind": "fixed", "start": "2023-01-01", "end": "2025-12-31"},
    "essential": True,
    "reason": "营业收入与归母净利润",
}


def test_metrics_become_a_facts_request_without_a_channel():
    need = _asked_need(ASKED, 1, 1)
    assert isinstance(need, FactRequest)
    dumped = need.model_dump(mode="json")
    assert dumped["kind"] == "facts" and dumped["codes"] == ASKED["metrics"]
    assert (need.consolidation, need.duration, need.disclosure) == (
        "consolidated",
        "year",
        "first",
    )
    assert need.required is True
    assert "source_query" not in dumped and "doc_type" not in dumped


def test_metrics_need_a_fixed_period_and_known_code_shapes():
    with pytest.raises(DataChoiceInvalid):
        _asked_need({**ASKED, "period": {"kind": "latest", "count": 3}}, 1, 1)
    with pytest.raises(DataChoiceInvalid):
        _asked_need({**ASKED, "metrics": ["营业收入"]}, 1, 1)


def test_documents_are_still_documents():
    need = _asked_need(
        {
            "subject": "300750.SZ",
            "doc_type": "annual_report",
            "period": {"kind": "latest", "count": 1},
            "reason": "年报原件",
        },
        1,
        2,
    )
    assert isinstance(need, DataRequest)


def test_stored_and_revised_facts_read_back_as_facts():
    need = _asked_need(ASKED, 1, 1)
    stored = {"needs": [need.model_dump(mode="json")]}
    assert isinstance(request_needs(stored)[0], FactRequest)
    revised = _revised([{**need.model_dump(mode="json"), "end": "2024-12-31"}], "r1")
    assert isinstance(revised[0], FactRequest)
    assert str(revised[0].end) == "2024-12-31"


def test_a_facts_item_label_shows_the_period_end():
    need = _asked_need(ASKED, 1, 1)
    item = {
        "key": {"kind": "facts", "subject": "300750.SZ", "period_end": "2025-12-31"}
    }
    assert _label(need, item) == "营业收入与归母净利润 300750.SZ 2025-12-31"
