"""Matching guards prevent silent narrowing, invented evidence and paid summons."""

from copy import deepcopy

import pytest

from app.domain.workbench.expert_match import (
    DirectorySnapshot,
    ExpertMatch,
    MatchReceipt,
    requirement_capabilities,
    validate_match,
    verify_receipt,
)

QUESTION = "核对文件并复核，交付报告。"


def directory():
    return {
        "contract": "knowmart/expert-directory/v2",
        "complete": True,
        "experts": [
            {
                "knowbot_id": "synthetic",
                "version": "1",
                "digest": "sha256:" + "a" * 64,
                "name": "文档核对",
                "tagline": "按作者方法核对",
                "scope": {"does": "核对并复核文档", "does_not": "", "elsewhere": ""},
                "declaration": "capabilities/v1",
                "runtime_contract": "knowbench/dynamic-methods/v3",
                "public_capabilities": [
                    {
                        "id": "double_check",
                        "statement": "核对并复核文档",
                        "conditions": [],
                        "exclusions": [],
                        "delivers": ["报告"],
                    }
                ],
                "free": True,
                "price": "0.00",
            }
        ],
    }


def result():
    return {
        "contract": "knowbench/expert-match/v2",
        "requirements": [
            {
                "id": "r1",
                "kind": "result",
                "statement": "核对后复核",
                "question_quote": "核对文件并复核",
                "required": True,
            },
            {
                "id": "r2",
                "kind": "constraint",
                "statement": "提供报告",
                "question_quote": "交付报告",
                "required": True,
            },
        ],
        "outcome": "summon",
        "picks": [
            {
                "knowbot_id": "synthetic",
                "version": "1",
                "reason": "作者支持本题所需核对及交付",
                "unresolved_conditions": [],
                "coverage": [
                    {
                        "requirement_id": "r1",
                        "status": "covered",
                        "capability_id": "double_check",
                        "declaration_quote": "核对并复核文档",
                        "reason": "覆盖核对与复核",
                    },
                    {
                        "requirement_id": "r2",
                        "status": "covered",
                        "capability_id": "double_check",
                        "declaration_quote": "报告",
                        "reason": "声明交付报告",
                    },
                ],
            }
        ],
        "clarification": None,
        "recommend": None,
        "demand": None,
    }


def validate(doc=None, entries=None):
    return validate_match(
        QUESTION,
        ExpertMatch.model_validate(doc or result()),
        DirectorySnapshot.model_validate(entries or directory()),
    )


def test_receipt_is_question_and_public_version_bound():
    receipt = validate()
    assert "method_sources" not in receipt.model_dump_json()
    verify_receipt(receipt, QUESTION)
    assert requirement_capabilities(
        receipt,
        knowbot_id="synthetic",
        version="1",
        pack_digest="sha256:" + "a" * 64,
        question=QUESTION,
    ) == {"r1": {"double_check"}, "r2": {"double_check"}}
    with pytest.raises(ValueError, match="different expert version"):
        requirement_capabilities(
            receipt,
            knowbot_id="synthetic",
            version="2",
            pack_digest="sha256:" + "a" * 64,
            question=QUESTION,
        )
    forged = receipt.model_dump(mode="json")
    forged["directory_digest"] = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="receipt"):
        verify_receipt(MatchReceipt.model_validate(forged), QUESTION)
    with pytest.raises(ValueError):
        verify_receipt(receipt, "只核对")


@pytest.mark.parametrize(
    "damage",
    [
        "invented_requirement",
        "missing_coverage",
        "duplicate_coverage",
        "unknown_expert",
        "unknown_version",
        "invented_capability",
        "invented_quote",
        "unknown",
        "uncovered",
        "unresolved",
        "no_capability",
    ],
)
def test_model_cannot_turn_missing_or_unknown_coverage_into_a_summon(damage):
    doc = result()
    pick = doc["picks"][0]
    row = pick["coverage"][0]
    if damage == "invented_requirement":
        doc["requirements"][0]["question_quote"] = "使用作者没有声明的算法"
    elif damage == "missing_coverage":
        pick["coverage"].pop()
    elif damage == "duplicate_coverage":
        pick["coverage"].append(deepcopy(row))
    elif damage == "unknown_expert":
        pick["knowbot_id"] = "invented"
    elif damage == "unknown_version":
        pick["version"] = "2"
    elif damage == "invented_capability":
        row["capability_id"] = "invented"
    elif damage == "invented_quote":
        row["declaration_quote"] = "作者说可以任意计算"
    elif damage in {"unknown", "uncovered"}:
        row["status"] = damage
    elif damage == "unresolved":
        pick["unresolved_conditions"] = ["是否包括复核尚不明确"]
    else:
        row["capability_id"] = None
    with pytest.raises(ValueError):
        validate(doc)


def test_paid_match_is_a_candidate_and_never_an_automatic_summon():
    entries = directory()
    entries["experts"][0].update(free=False, price="5.00")
    with pytest.raises(ValueError, match="paid experts"):
        validate(entries=entries)
    doc = result()
    doc["outcome"] = "candidates"
    assert validate(doc, entries).result.outcome == "candidates"


def test_partial_match_can_explain_gaps_without_inviting():
    doc = result()
    doc["outcome"] = "none"
    doc["recommend"] = {
        "action": "incubate",
        "reason": "尚缺复核能力，请选其他专家或提交孵化请求",
    }
    doc["picks"][0]["coverage"][0]["status"] = "unknown"
    receipt = validate(doc)
    with pytest.raises(ValueError, match="does not authorize"):
        requirement_capabilities(
            receipt,
            knowbot_id="synthetic",
            version="1",
            pack_digest="sha256:" + "a" * 64,
            question=QUESTION,
        )


@pytest.mark.parametrize("outcome", ["none", "summon", "candidates", "clarify"])
def test_incomplete_directory_is_never_a_matching_result(outcome):
    entries = directory()
    entries["complete"] = False
    doc = result()
    doc["outcome"] = outcome
    if outcome == "none":
        doc["picks"] = []
        doc["recommend"] = {"action": "direct", "reason": "可自行回答"}
    if outcome == "clarify":
        doc["clarification"] = "需要复核吗？"
    with pytest.raises(ValueError, match="incomplete"):
        validate(doc, entries)


def test_directory_rejects_private_or_unknown_fields():
    entries = directory()
    entries["experts"][0]["method_sources"] = {"private": "not public"}
    with pytest.raises(ValueError):
        validate(entries=entries)


def test_old_scope_can_match_but_cannot_invent_a_capability_plan():
    entries = directory()
    entry = entries["experts"][0]
    entry.update(
        declaration="scope/v1",
        runtime_contract="knowbench/fixed/v1",
        public_capabilities=[],
    )
    doc = result()
    for row in doc["picks"][0]["coverage"]:
        row.update(capability_id=None, declaration_quote="核对并复核文档")
    receipt = validate(doc, entries)
    with pytest.raises(ValueError, match="no complete capability"):
        requirement_capabilities(
            receipt,
            knowbot_id="synthetic",
            version="1",
            pack_digest=entry["digest"],
            question=QUESTION,
        )
