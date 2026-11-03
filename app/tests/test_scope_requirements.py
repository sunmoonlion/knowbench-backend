import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.application.workbench.acceptance import out_of_scope
from app.application.workbench.advisor import Advisor, compose_data_plan
from app.domain.workbench.expert_match import Requirement
from app.domain.workbench.packs import SMOKE, Scope


def test_declared_scope_precedes_data_discovery_and_partial_fit_is_not_permission():
    pack = SMOKE.model_copy(
        update={"internal": False, "scope": Scope(does="季度同比", does_not="DCF")}
    )
    prompt = compose_data_plan(pack, {"text": "用DCF估值"}, "task-1", 0)
    assert prompt.index("First: is this question") < prompt.index(
        "## How this expert works"
    )
    assert "Exclusions take precedence" in prompt
    assert "do not request data" in prompt
    assert '"proposed_question"' in prompt
    step_prompt = Advisor.compose(pack.workflow[0], {}, {}, 0, intake=pack.scope)
    assert "partly covered question is NOT" not in step_prompt
    assert "No answer, cancellation or timeout is not consent" in step_prompt


def test_explicit_method_cannot_be_demoted_to_optional():
    with pytest.raises(ValidationError, match="cannot become optional"):
        Requirement(
            id="method",
            kind="explicit_method",
            statement="DCF",
            question_quote="DCF",
            required=False,
        )


@pytest.mark.parametrize("proposed", ["", None, [], "x" * 4001])
def test_no_valid_smaller_question_cannot_offer_a_blanket_override(proposed):
    result = out_of_scope(
        json.dumps(
            {"out_of_scope": True, "reason": "不支持DCF", "proposed_question": proposed}
        )
    )
    assert result is not None and result["proposed_question"] == ""


@pytest.mark.parametrize("decision", [None, "rework", "proceed"])
async def test_only_explicit_proceed_carries_concrete_scope_to_later_steps(decision):
    subject = {
        "step_id": "data-plan",
        "out_of_scope": True,
        "proposed_question": "只计算历史营收",
    }
    repo = SimpleNamespace(
        list_interactions=AsyncMock(
            return_value=[
                {
                    "task_id": "task-1",
                    "kind": "input",
                    "prompt": {"subject": subject},
                    "response": {"decision": decision},
                }
            ]
        )
    )
    advisor = Advisor(None, None)
    answer = await advisor._answers_for_step(
        repo, {"id": "task-1", "session_id": "session-1"}, SMOKE.workflow[-1]
    )
    assert answer["proceed"] is (decision == "proceed")
    if decision == "proceed":
        assert "只计算历史营收" in answer["change_request"]
        assert "disclose omitted results" in answer["change_request"]
    else:
        assert answer["change_request"] is None
