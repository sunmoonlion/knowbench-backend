"""A non-advice declaration is not advice and cannot hide advice elsewhere."""

import json

import pytest

from app.application.workbench.acceptance import judge
from app.domain.workbench.packs import AcceptanceRule

RULE = AcceptanceRule(kind="no_positioning_advice")
DECLARATION = "本文只描述数据，不评价好坏；不给买卖建议、评级、目标价，结论留给用户。"


@pytest.mark.parametrize(
    "report",
    [
        DECLARATION,
        "本文不含评级、目标价或买卖建议，如何判断留给您。",
        "本报告不含目标价。",
        "本报告不提供目标价。",
        "不提供任何买入评级或卖出评级及目标价。",
        "本文不给出投资建议、投资评级和目标价。",
        "不给买卖建议、评级、目标价",
        "# 局限\n- " + DECLARATION + "\n\n## 出处\n年报第5页。",
        "数据范围有限。我们不提供目标价。其他数字均来自年报。",
        "不提供目标价。不给出买入评级。",
    ],
)
def test_complete_explicit_declarations_pass_without_changing_report(report):
    content = {"report": report}
    result = judge(json.dumps(content, ensure_ascii=False), (RULE,))
    assert result.ok
    assert result.parsed == content
    assert result.checks[0]["detail"] == ""


@pytest.mark.parametrize(
    "report",
    [
        "目标价1800元。",
        "本文不含目标价1800元。",
        "本文不含目标价，但建议买入。",
        "本文不含评级、目标价或买卖建议，如何判断留给您，建议买入。",
        "本文不含评级、目标价或买卖建议，如何判断留给您。目标价1800元。",
        "并非不含目标价。",
        "不提供目标价1800元。",
        "不提供目标价：1800元。",
        "不给买卖建议、评级、目标价1800元，结论留给用户。",
        "不提供目标价，但可参考1800元。",
        "并非不提供目标价。",
        "不能不给目标价。",
        "建议买入。",
        "不提供建议买入评级。",
        DECLARATION + "目标价1800元。",
        "目标价1800元。" + DECLARATION,
        DECLARATION + "\n建议买入。",
        DECLARATION + "本股必涨。",
        DECLARATION + "Strong Buy",
        DECLARATION + "price target 1800",
        "不给买卖建议、评级、目标价，结论留给用户，建议买入。",
    ],
)
def test_actual_advice_or_ambiguous_negation_stays_rejected(report):
    result = judge(json.dumps({"report": report}, ensure_ascii=False), (RULE,))
    assert not result.ok
    assert result.checks[0]["detail"]


@pytest.mark.parametrize(
    "content",
    [
        {"report": DECLARATION, "appendix": "目标价1800元"},
        {"report": DECLARATION, "tables": [{"rows": [{"note": "建议买入"}]}]},
        {"parts": ["不提供", "目标价"]},
        {"parts": ["不提供目标价", "1800元为目标价"]},
    ],
)
def test_disclaimer_cannot_exempt_another_field_or_list_item(content):
    assert not judge(json.dumps(content, ensure_ascii=False), (RULE,)).ok


def test_scoped_rule_and_other_acceptance_checks_keep_their_meaning():
    content = {"report": DECLARATION, "outside_scope": "目标价1800元"}
    scoped = AcceptanceRule(kind="no_positioning_advice", path="report")
    assert judge(json.dumps(content, ensure_ascii=False), (scoped,)).ok
    required = AcceptanceRule(kind="required_keys", keys=("missing",))
    result = judge(json.dumps(content, ensure_ascii=False), (scoped, required))
    assert not result.ok
    assert result.checks[0]["pass"] is True
    assert result.checks[1]["pass"] is False
