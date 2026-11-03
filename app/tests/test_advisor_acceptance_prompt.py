"""The first attempt must receive the same frozen checks as its evaluator."""

import json

from app.application.workbench.acceptance import judge
from app.application.workbench.acceptance_numbers import _covers
from app.application.workbench.advisor import Advisor
from app.domain.workbench.packs import AcceptanceRule, StepContract


def test_initial_prompt_contains_exact_frozen_rules_without_mutating_them():
    rules = (
        AcceptanceRule(kind="json_object"),
        AcceptanceRule(kind="each_has", path="rows", field="metric", values=("a", "b")),
        AcceptanceRule(kind="computed", path="rows", tolerance=0.0000005),
        AcceptanceRule(
            kind="covers", path="comparisons", field="conclusion", source="up.claims"
        ),
    )
    step = StepContract(
        step_id="check",
        title="Check",
        method_text="Author method",
        output_artifact="result",
        acceptance=rules,
    )
    before = step.model_dump_json()
    prompt = Advisor.compose(step, {}, {}, 0)
    encoded = prompt.split("## Acceptance rules for this frozen step\n", 1)[
        1
    ].splitlines()[-1]
    decoded = tuple(AcceptanceRule.model_validate(r) for r in json.loads(encoded))
    assert decoded == rules
    assert "without paraphrasing, splitting or combining" in prompt
    assert step.model_dump_json() == before
    assert "rework #" not in prompt


def test_missing_brace_feedback_reaches_rework_and_coverage_still_enforced():
    coverage = AcceptanceRule(
        kind="covers", path="comparisons", field="conclusion", source="up.claims"
    )
    step = StepContract(
        step_id="check",
        title="Check",
        method_text="Author method",
        output_artifact="result",
        acceptance=(AcceptanceRule(kind="json_object"), coverage),
    )
    raw = '{"comparisons":[{"conclusion":"rewritten claim"}]'
    verdict = judge(raw, step.acceptance)
    prompt = Advisor.compose(step, {}, {}, 1, rework_failures=verdict.failures)
    assert "expected closing delimiters: }" in prompt
    assert "What failed last time" in prompt
    # Correct syntax alone cannot turn altered upstream identities into a pass.
    corrected = judge(raw + "}", step.acceptance)
    assert corrected.ok
    upstream = {"up": {"claims": ["original claim"]}}
    assert _covers(coverage, corrected.parsed, upstream)
    assert not _covers(
        coverage, {"comparisons": [{"conclusion": "original claim"}]}, upstream
    )
