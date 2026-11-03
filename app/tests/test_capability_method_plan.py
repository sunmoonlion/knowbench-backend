"""Capability coverage and optional inputs use the existing execution projection."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from method_plan_fixtures import catalog as legacy_catalog
from method_plan_fixtures import proposal as legacy_proposal
from pydantic import ValidationError

from app.domain.workbench.execution_plan import project_plan
from app.domain.workbench.method_catalog import (
    CapabilityMethodCatalog,
    parse_catalog,
    render,
)
from app.domain.workbench.method_plan import (
    AcceptedMethodPlan,
    CapabilityMethodPlan,
    parse_plan,
    validate_plan,
)


def test_capability_provider_schema_is_exact():
    path = (
        Path(__file__).resolve().parents[3]
        / "contracts/dynamic-methods/v3/catalog.schema.json"
    )
    assert path.read_text() == render(capabilities=True)


@pytest.mark.parametrize("review", [False, True])
def test_actual_knowlab_compiler_output_runs_both_task_plans(review):
    path = Path(__file__).parent / "fixtures/capability-methods-manifest.json"
    manifest = json.loads(path.read_text())
    doc = proposal(review=review)
    report = doc["steps"][-1]
    report["inputs"][0]["slot"] = "inspect"
    if review:
        report["inputs"][-1]["slot"] = "review"
    else:
        report["omitted_inputs"] = {"review": "not_selected"}
    plan = validate(doc, cat=manifest["pack"])
    view = project_plan(
        parse_catalog(manifest["pack"]),
        AcceptedMethodPlan(
            task_id="synthetic",
            attempt_id="plan",
            pack_digest="sha256:" + "a" * 64,
            version=1,
            proposal=plan,
        ),
        profile_id="synthetic",
    )
    assert len(view.pack.workflow) == (3 if review else 2)


def document():
    doc = legacy_catalog().model_dump(mode="json")
    doc.update(
        runtime_contract="knowbench/dynamic-methods/v3",
        method_compiler="knowlab/workdraft-compiler/v2",
        method_sources={},
        public_capabilities=[
            dict(
                id="check",
                statement="核对给定文档",
                conditions=[],
                exclusions=[],
                delivers=["核对报告"],
            ),
            dict(
                id="double_check",
                statement="核对并复核给定文档",
                conditions=[],
                exclusions=[],
                delivers=["含复核的报告"],
            ),
        ],
    )
    doc["planning"]["capability_bindings"] = {
        "check": dict(
            required_units=["inspect", "report"],
            evidence_outputs=[dict(method="report", field="rows")],
        ),
        "double_check": dict(
            required_units=["inspect", "review", "report"],
            evidence_outputs=[dict(method="report", field="rows")],
        ),
    }
    for unit in doc["method_units"]:
        step = unit["contract"]
        step["input_slots"] = {
            slot: dict(method_id=slot, required=True) for slot in step["input_refs"]
        }
    report = doc["method_units"][0]["contract"]
    report["input_refs"] = ["records", "verification"]
    report["input_slots"] = dict(
        records=dict(method_id="inspect", required=True),
        verification=dict(method_id="review", required=False),
    )
    report["back_to"] = "records"
    report["output_schema"]["review_summary"] = "string"
    for rule in report["acceptance"]:
        if rule["kind"] == "covers":
            rule["source"] = "records.rows"
        if rule["kind"] == "required_keys":
            rule["keys"].append("review_summary")
    report["acceptance"].append(
        dict(kind="non_empty", path="review_summary", when_input_present="verification")
    )
    return doc


def proposal(*, review=False):
    doc = legacy_proposal(review=review).model_dump(mode="json")
    doc["contract"] = "knowbench/method-plan/v2"
    doc["requirement_coverage"] = {"r1": "double_check" if review else "check"}
    doc["unselected_methods"] = {} if review else {"review": "本题没有要求复核"}
    for step in doc["steps"]:
        step["omitted_inputs"] = {}
    report = doc["steps"][-1]
    report["inputs"][0]["slot"] = "records"
    if review:
        report["inputs"].append(dict(slot="verification", instance_id="review_1"))
    else:
        report["omitted_inputs"] = {"verification": "not_selected"}
    return doc


def validate(doc, *, cat=None, requirements=None):
    plan = parse_plan(doc)
    validate_plan(
        parse_catalog(cat or document()),
        plan,
        previous=None,
        protected_prefix=0,
        completed_prefix=0,
        results={},
        available_tools={"read_input"},
        requirement_capabilities=requirements or {"r1": {"check", "double_check"}},
    )
    return plan


@pytest.mark.parametrize("review", [False, True])
def test_same_catalog_selects_different_closed_plans_and_projects_old_executor(review):
    cat = parse_catalog(document())
    assert isinstance(cat, CapabilityMethodCatalog)
    plan = validate(proposal(review=review))
    assert isinstance(plan, CapabilityMethodPlan)
    before = cat.model_dump(mode="json")
    view = project_plan(
        cat,
        AcceptedMethodPlan(
            task_id="synthetic",
            attempt_id="plan-attempt",
            pack_digest="sha256:" + "a" * 64,
            version=1,
            proposal=plan,
        ),
        profile_id="synthetic",
    )
    assert len(view.pack.workflow) == (3 if review else 2)
    report = view.pack.workflow[-1]
    assert len(report.input_refs) == (2 if review else 1)
    assert any(rule.kind == "non_empty" for rule in report.acceptance) is review
    assert any(rule.kind == "covers" for rule in report.acceptance)
    assert report.back_to == "inspect_1"
    assert cat.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("missing_slot", "bind or explicitly omit"),
        ("required_omitted", "required input"),
        ("selected_omitted", "selected upstream"),
        ("wrong_source", "earlier matching"),
        ("missing_capability_method", "required methods"),
        ("unknown_capability", "unknown author capability"),
        ("unexplained_method", "explain every"),
        ("invented_requirement", "authoritative task requirements"),
        ("wrong_requirement_capability", "accepted requirement"),
        ("wrong_plan_version", "contract"),
        ("dangling_result", "final deliverable"),
    ],
)
def test_invalid_compositions_cannot_become_successful_reports(mutation, match):
    doc = proposal()
    cat = document()
    requirements = None
    report = doc["steps"][-1]
    if mutation == "missing_slot":
        report["omitted_inputs"] = {}
    elif mutation == "required_omitted":
        report["inputs"] = []
        report["omitted_inputs"]["records"] = "not_selected"
    elif mutation == "selected_omitted":
        doc = proposal(review=True)
        doc["steps"][-1]["inputs"].pop()
        doc["steps"][-1]["omitted_inputs"] = {"verification": "not_selected"}
    elif mutation == "wrong_source":
        doc = proposal(review=True)
        doc["steps"][-1]["inputs"][0]["instance_id"] = "review_1"
    elif mutation == "missing_capability_method":
        doc["requirement_coverage"]["r1"] = "double_check"
    elif mutation == "unknown_capability":
        doc["requirement_coverage"]["r1"] = "invented"
    elif mutation == "unexplained_method":
        doc["unselected_methods"] = {}
    elif mutation == "invented_requirement":
        doc["requirement_coverage"] = {"easy_question": "check"}
    elif mutation == "wrong_requirement_capability":
        requirements = {"r1": {"double_check"}}
    elif mutation == "wrong_plan_version":
        doc = legacy_proposal().model_dump(mode="json")
    else:
        extra = deepcopy(doc["steps"][0])
        extra["instance_id"] = "unused_inspection"
        doc["steps"].insert(1, extra)
    with pytest.raises(ValueError, match=match):
        validate(doc, cat=cat, requirements=requirements)


@pytest.mark.parametrize(
    "mutation",
    [
        "mandatory_guard",
        "wrong_guard",
        "optional_back",
        "capability_without_method",
        "duplicate_capability",
        "unknown_output",
        "capability_over_budget",
        "cycle",
    ],
)
def test_author_catalog_rejects_impossible_or_weakened_contracts(mutation):
    doc = document()
    report = doc["method_units"][0]["contract"]
    if mutation == "mandatory_guard":
        report["acceptance"][0]["when_input_present"] = "verification"
    elif mutation == "wrong_guard":
        report["acceptance"][-1]["when_input_present"] = "records"
    elif mutation == "optional_back":
        report["back_to"] = "verification"
    elif mutation == "capability_without_method":
        doc["planning"]["capability_bindings"].pop("check")
    elif mutation == "duplicate_capability":
        doc["public_capabilities"].append(doc["public_capabilities"][0])
    elif mutation == "unknown_output":
        doc["planning"]["capability_bindings"]["check"]["evidence_outputs"][0][
            "field"
        ] = "invented"
    elif mutation == "capability_over_budget":
        doc["planning"]["max_steps"] = 2
    else:
        report["input_slots"]["verification"]["method_id"] = "report"
    with pytest.raises(ValidationError):
        parse_catalog(doc)


def test_v1_cannot_accept_optional_or_capability_semantics():
    doc = document()
    doc["runtime_contract"] = "knowbench/dynamic-methods/v1"
    with pytest.raises(ValidationError):
        parse_catalog(doc)


def test_plan_acceptance_requires_external_requirement_evidence():
    with pytest.raises(ValueError, match="authoritative task requirements"):
        validate_plan(
            parse_catalog(document()),
            parse_plan(proposal()),
            previous=None,
            protected_prefix=0,
            completed_prefix=0,
            results={},
            available_tools={"read_input"},
        )


@pytest.mark.parametrize("review", [False, True])
def test_independent_optional_method_definition_uses_the_same_planner(review):
    path = Path(__file__).parent / "fixtures/capability-referenced-manifest.json"
    manifest = json.loads(path.read_text())
    plan = validate(proposal(review=review), cat=manifest["pack"])
    view = project_plan(
        parse_catalog(manifest["pack"]),
        AcceptedMethodPlan(
            task_id="synthetic",
            attempt_id="plan",
            pack_digest="sha256:" + "a" * 64,
            version=1,
            proposal=plan,
        ),
        profile_id="synthetic",
    )
    assert len(view.pack.workflow) == (3 if review else 2)
