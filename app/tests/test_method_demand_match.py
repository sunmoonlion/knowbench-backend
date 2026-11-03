"""Method suggestions must not become hard constraints or author capabilities."""

from copy import deepcopy

import pytest

from app.domain.workbench.expert_match import (
    DirectorySnapshot,
    ExpertMatchV3,
    parse_match,
    validate_match,
    verify_receipt,
)
from app.domain.workbench.packs import find_pack
from app.domain.workbench.task_requirements import TaskRequirements, from_match
from tests.test_expert_match import QUESTION, directory, result


def demand_result():
    doc = result()
    doc["contract"] = "knowbench/expert-match/v3"
    for req in doc["requirements"]:
        req.update(origin="user_explicit", rationale="来自用户原问题")
    doc["method_proposals"] = [
        {
            "id": "compare",
            "requirement_ids": ["r1"],
            "approach": "逐项对照原文件后复核",
            "reason": "帮助完成用户要求的核对",
            "data_requirements": ["可读原文件"],
        }
    ]
    doc["picks"][0]["solution"] = {
        "approach": "按作者核对能力复核并交付报告",
        "method_proposal_ids": [],
        "data_conditions": ["执行前确认原文件读取权限"],
    }
    return doc


def receipt(doc=None):
    return validate_match(
        QUESTION,
        parse_match(doc or demand_result()),
        DirectorySnapshot.model_validate(directory()),
    )


def test_unused_suggestion_does_not_block_expert_or_enter_required_coverage():
    matched = receipt()
    assert matched.contract == "knowbench/expert-match-receipt/v3"
    verify_receipt(matched, QUESTION)
    accepted = from_match(
        matched,
        match_task_id="test",
        match_artifact_version=1,
        knowbot_id="synthetic",
        version="1",
        expert_digest="sha256:" + "a" * 64,
        question=QUESTION,
    )
    assert accepted.capability_mapping.keys() == {"r1", "r2"}
    assert accepted.method_proposals[0].id == "compare"
    assert accepted.solution and not accepted.solution.method_proposal_ids
    restored = TaskRequirements.model_validate(accepted.model_dump(mode="json"))
    assert restored == accepted
    assert restored.requirements[0].origin == "user_explicit"
    assert restored.digest() == accepted.digest()


@pytest.mark.parametrize(
    "damage",
    [
        "invented_requirement",
        "duplicate_proposal",
        "proposal_as_requirement",
        "unknown_solution",
        "blank_rationale",
        "optional_explicit_method",
        "inferred_explicit_method",
    ],
)
def test_invalid_method_demand_or_solution_is_rejected(damage):
    doc = demand_result()
    if damage == "invented_requirement":
        doc["method_proposals"][0]["requirement_ids"] = ["invented"]
    elif damage == "duplicate_proposal":
        doc["method_proposals"].append(deepcopy(doc["method_proposals"][0]))
    elif damage == "proposal_as_requirement":
        doc["method_proposals"][0]["id"] = "r1"
    elif damage == "unknown_solution":
        doc["picks"][0]["solution"]["method_proposal_ids"] = ["invented"]
    elif damage == "blank_rationale":
        doc["requirements"][0]["rationale"] = " "
    else:
        doc["requirements"][0]["kind"] = "explicit_method"
        if damage == "optional_explicit_method":
            doc["requirements"][0]["required"] = False
        else:
            doc["requirements"][0]["origin"] = "goal_inferred"
    with pytest.raises(ValueError):
        receipt(doc)


def test_explicit_method_not_covered_cannot_be_replaced_by_a_proposal():
    doc = demand_result()
    doc["requirements"][0]["kind"] = "explicit_method"
    doc["picks"][0]["coverage"][0]["status"] = "uncovered"
    doc["picks"][0]["solution"]["method_proposal_ids"] = ["compare"]
    with pytest.raises(ValueError, match="partial"):
        receipt(doc)


def test_old_finder_and_receipt_remain_versioned():
    assert parse_match(result()).model_dump(mode="json") == result()
    with pytest.raises(ValueError):
        ExpertMatchV3.model_validate(result())
    old, new = find_pack("FIND_EXPERT", "2"), find_pack("FIND_EXPERT", "3")
    assert old and new
    assert old.workflow[0].output_schema["contract"] == "knowbench/expert-match/v2"
    assert "method_proposals" not in old.workflow[0].output_schema
    assert "method_proposals" in new.workflow[0].output_schema
