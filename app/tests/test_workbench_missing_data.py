"""「没有数据」怎么认（PRD/apps/investment.md 7.5、F-PROJ-07）。"""

from __future__ import annotations

import pytest
from sample_packs import FIN_REVIEW

from app.domain.workbench.missing_data import from_step, from_tool_call, security_of

PROFILE_STEP = next(s for s in FIN_REVIEW.workflow if s.step_id == "profile")
SCOPE_STEP = next(s for s in FIN_REVIEW.workflow if s.step_id == "scope")


@pytest.mark.parametrize(
    ("dataset", "expected"),
    [
        ("sh600519-financials", {"security_code": "600519", "market": "sh"}),
        ("SZ000001-financials-abc", {"security_code": "000001", "market": "sz"}),
        ("bj830799-financials", {"security_code": "830799", "market": "bj"}),
        (" sh600276-financials-5868ab1a ", {"security_code": "600276", "market": "sh"}),
        ("sh6005190-financials", None),  # 七位数字不是证券代码
        ("retail", None),
        ("600519", None),  # 没有市场：不猜
        ("financials-sh600519", None),
        ("", None),
        (None, None),
        (600519, None),
    ],
)
def test_the_security_in_a_dataset_name(dataset, expected):
    assert security_of(dataset) == expected


def call(dataset="sh600519-financials", status="failed", text=None, error=None, **more):
    said = (
        text if text is not None else f"unknown dataset: {dataset}; call list_datasets"
    )
    return {
        "type": "mcpToolCall",
        "server": "knowbase",
        "tool": "run_sql",
        "status": status,
        "arguments": {"dataset": dataset, "sql": "select 1"},
        "result": None if error else {"content": [{"type": "text", "text": said}]},
        "error": {"message": error} if error else None,
        **more,
    }


def test_a_tool_that_says_there_is_no_such_dataset():
    assert from_tool_call(call()) == {
        "security_code": "600519",
        "market": "sh",
        "dataset": "sh600519-financials",
        "source": "tool",
    }
    # 答复在 error 里也认
    assert from_tool_call(call(error="Unknown dataset: sh600519-financials"))


@pytest.mark.parametrize(
    "item",
    [
        call(status="completed"),  # 查成了
        call(dataset="retail"),  # 认不出代码
        call(text="SELECT only"),  # 是别的错
        call(error="connection refused"),
        {**call(), "type": "commandExecution"},
        {**call(), "arguments": None},
        {**call(), "arguments": "sh600519-financials"},
        None,
        "x",
    ],
)
def test_what_does_not_count(item):
    assert from_tool_call(item) is None


def test_a_step_that_reports_a_company_not_ingested():
    assert from_step(
        PROFILE_STEP,
        {"dataset": None, "not_ingested": True, "security_code": "600519"},
    ) == {
        "security_code": "600519",
        "market": None,
        "dataset": None,
        "source": "expert",
    }
    # 没有写 not_ingested，但数据集是空的：也算
    assert from_step(PROFILE_STEP, {"dataset": "", "security_code": "600519"})


@pytest.mark.parametrize(
    "returned",
    [
        {"dataset": "sh600519-financials-1", "security_code": "600519"},  # 有数据
        {"dataset": None, "not_ingested": True},  # 没写代码
        {"dataset": None, "not_ingested": True, "security_code": "茅台"},
        {"dataset": None, "not_ingested": True, "security_code": 600519},
        None,
        "not json",
    ],
)
def test_a_step_that_does_not_count(returned):
    assert from_step(PROFILE_STEP, returned) is None


def test_only_steps_that_say_so_can_find_data_missing():
    assert SCOPE_STEP.missing_data is None
    assert from_step(SCOPE_STEP, {"dataset": None, "security_code": "600519"}) is None
    declared = [s.step_id for s in FIN_REVIEW.workflow if s.missing_data]
    assert declared == ["profile"]
