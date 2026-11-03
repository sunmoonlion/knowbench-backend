"""Current selection is grounded judgment, never an invented coverage proof."""

from copy import deepcopy

import pytest

from app.domain.workbench.expert_match import (
    DirectorySnapshot,
    parse_match,
    validate_match,
    verify_receipt,
)
from app.domain.workbench.packs import find_pack
from app.domain.workbench.task_requirements import from_manual, from_match
from tests.test_expert_match import QUESTION, directory, result


def declaration_result():
    old = result()
    p = old["picks"][0]
    return {
        **{k: v for k, v in old.items() if k != "picks"},
        "contract": "knowbench/expert-match/v4",
        "picks": [
            {
                "knowbot_id": p["knowbot_id"],
                "version": p["version"],
                "reason": "公开说明适合用户目的；执行时核对实际资料。",
                "fit": "suitable",
                "declarations": [
                    {
                        "capability_id": c["capability_id"],
                        "quote": c["declaration_quote"],
                    }
                    for c in p["coverage"]
                ],
                "limitations": ["尚未验证各方法组合能完成全部目标；数据待用户授权。"],
            }
        ],
    }


def receipt(doc=None, entries=None):
    return validate_match(
        QUESTION,
        parse_match(doc or declaration_result()),
        DirectorySnapshot.model_validate(entries or directory()),
    )


def test_uncertain_combination_is_disclosed_not_used_as_invitation_gate():
    accepted = receipt()
    verify_receipt(accepted, QUESTION)
    assert accepted.contract.endswith("/v4")
    assert accepted.result.outcome == "summon"
    dumped = accepted.result.model_dump(mode="json")
    assert "method_proposals" not in dumped
    assert "coverage" not in dumped["picks"][0]
    task = from_match(
        accepted,
        match_task_id="round",
        match_artifact_version=1,
        knowbot_id="synthetic",
        version="1",
        expert_digest="sha256:" + "a" * 64,
        question=QUESTION,
    )
    assert task.selection_basis == "declaration"
    assert task.limitations == tuple(dumped["picks"][0]["limitations"])
    assert not task.method_proposals and task.solution is None
    assert set(task.capability_mapping) == {"r1", "r2"}


@pytest.mark.parametrize(
    "damage",
    [
        "invented_quote",
        "unknown_capability",
        "unknown_expert",
        "known_mismatch",
        "ambiguous",
        "paid",
        "incomplete",
        "invented_requirement",
        "optional_algorithm",
    ],
)
def test_identity_permissions_and_known_conflicts_remain_enforced(damage):
    doc, entries = declaration_result(), directory()
    pick = doc["picks"][0]
    if damage == "invented_quote":
        pick["declarations"][0]["quote"] = "not in declaration"
    if damage == "unknown_capability":
        pick["declarations"][0]["capability_id"] = "invented"
    if damage == "unknown_expert":
        pick["version"] = "999"
    if damage == "known_mismatch":
        pick["fit"] = "unsuitable"
    if damage == "ambiguous":
        pick["fit"] = "clarify"
    if damage == "paid":
        entries["experts"][0].update(free=False, price="9")
    if damage == "incomplete":
        entries["complete"] = False
    if damage == "invented_requirement":
        doc["requirements"][0]["question_quote"] = "not in question"
    if damage == "optional_algorithm":
        doc["requirements"][0].update(kind="explicit_method", required=False)
    with pytest.raises(ValueError):
        receipt(doc, entries)


def test_no_match_keeps_user_remedies_and_legacy_rules_unchanged():
    doc = declaration_result()
    doc.update(
        outcome="none",
        recommend={"action": "incubate", "reason": "需要作者提供适合的方法"},
    )
    doc["picks"][0]["fit"] = "unsuitable"
    assert receipt(doc).result.outcome == "none"
    old = deepcopy(result())
    old["picks"][0]["unresolved_conditions"] = ["不能证明组合可行"]
    with pytest.raises(ValueError, match="partial or uncertain"):
        validate_match(
            QUESTION, parse_match(old), DirectorySnapshot.model_validate(directory())
        )
    assert (
        "method_proposals"
        not in find_pack("FIND_EXPERT", "4").workflow[0].output_schema
    )


def test_manual_selection_has_no_matching_round_and_keeps_whole_question():
    task = from_manual(
        question=QUESTION,
        expert_digest="sha256:" + "a" * 64,
        capabilities=("check", "review"),
    )
    assert task.match_task_id is None
    assert task.match_artifact_version is None
    assert task.selection_basis == "manual"
    assert task.requirements[0].statement == QUESTION
    task.verify(question=QUESTION, expert_digest="sha256:" + "a" * 64)
    with pytest.raises(ValueError):
        task.verify(question="changed", expert_digest="sha256:" + "a" * 64)


def test_manual_request_keeps_the_existing_four_thousand_character_limit():
    question = "核对财务数据" * 500
    task = from_manual(
        question=question, expert_digest="sha256:" + "a" * 64, capabilities=("check",)
    )
    assert task.requirements[0].question_quote == question
