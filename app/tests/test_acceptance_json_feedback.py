"""Malformed handbacks must give actionable feedback without being repaired."""

import json

import pytest

from app.application.workbench.acceptance import judge
from app.domain.workbench.packs import AcceptanceRule

RULES = (
    AcceptanceRule(kind="json_object"),
    AcceptanceRule(kind="required_keys", keys=("comparisons",)),
)


@pytest.mark.parametrize("prefix", ["", "已逐条对照。\n\n"])
def test_missing_outer_brace_is_reported_but_not_accepted(prefix):
    raw = prefix + '{"comparisons":[{"conclusion":"example","quotes":[]}]'
    result = judge(raw, RULES)
    assert not result.ok
    assert "expected closing delimiters: }" in result.failures[0]
    assert "line 1, column" in result.failures[0]
    assert result.parsed is None
    assert result.checks[1]["pass"] is None
    # Only a complete new handback may pass; the original still fails afterwards.
    assert judge(raw + "}", RULES).ok
    assert not judge(raw, RULES).ok


@pytest.mark.parametrize("quote", ["literal }", "literal {", 'quote " and }', '\\" {'])
def test_wrapped_json_ignores_delimiters_inside_citations(quote):
    content = {"comparisons": [{"quotes": [{"text": quote}]}]}
    raw = "交回如下：\n" + json.dumps(content) + "\n以上。"
    result = judge(raw, RULES)
    assert result.ok
    assert result.parsed == content


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"comparisons":[{"conclusion":"example"}', "expected closing delimiters: ]}"),
        ('{"comparisons":"unfinished', "unterminated string"),
        ('{"comparisons": [1,]}', "Expecting value"),
        ('{"comparisons" []}', "Expecting ':' delimiter"),
        ('{"comparisons": true false}', "Expecting ',' delimiter"),
        ("ordinary text", "no JSON object found"),
    ],
)
def test_other_syntax_errors_still_fail_without_fabricating_output(raw, reason):
    result = judge(raw, RULES)
    assert not result.ok
    assert reason in result.failures[0]
    assert result.parsed is None
    assert result.checks[1]["pass"] is None


def test_feedback_does_not_echo_citation_content_or_take_a_nested_object():
    raw = '{"comparisons":[{"text":"PRIVATE_SOURCE"}],"broken":}'
    result = judge(raw, RULES)
    assert not result.ok
    assert "PRIVATE_SOURCE" not in result.failures[0]
    assert "PRIVATE_SOURCE" not in result.checks[0]["detail"]
