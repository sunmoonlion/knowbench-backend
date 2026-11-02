import copy

import pytest
from method_plan_fixtures import catalog, proposal
from pydantic import ValidationError

from app.domain.workbench.knowbots import V2_TOOLS
from app.domain.workbench.method_plan import (
    AcceptedMethodPlan,
    MethodPlan,
    validate_plan,
)


def validate(plan, **overrides):
    context = dict(
        previous=None,
        protected_prefix=0,
        completed_prefix=0,
        results={},
        available_tools=V2_TOOLS,
    )
    context.update(overrides)
    validate_plan(catalog(), plan, **context)


def previous():
    return AcceptedMethodPlan(
        task_id="t",
        attempt_id="a",
        pack_digest="sha256:" + "a" * 64,
        version=1,
        proposal=proposal(),
    )


def test_same_expert_version_accepts_different_compositions():
    validate(proposal())
    validate(proposal(review=True))
    assert proposal().steps != proposal(review=True).steps


@pytest.mark.parametrize(
    "case",
    [
        "unknown_method",
        "missing_required",
        "tool_escalation",
        "forward_input",
        "wrong_slot",
        "missing_slot",
        "future_version",
        "unknown_final",
        "stale_parent",
        "call_limit",
        "missing_final",
    ],
)
def test_invalid_compositions_are_rejected(case):
    raw = proposal().model_dump(mode="json")
    if case == "unknown_method":
        raw["steps"][0]["method_id"] = "invented"
    elif case == "missing_required":
        raw["steps"] = raw["steps"][:1]
    elif case == "tool_escalation":
        raw["steps"][1]["tools"] = ["read_input"]
    elif case == "forward_input":
        raw["steps"].reverse()
    elif case == "wrong_slot":
        raw["steps"][1]["inputs"][0]["slot"] = "review"
    elif case == "missing_slot":
        raw["steps"][1]["inputs"] = []
    elif case == "future_version":
        raw["steps"][1]["inputs"][0]["version"] = 1
    elif case == "unknown_final":
        raw["final_outputs"] = {"report": "inspect_1"}
    elif case == "stale_parent":
        raw["parent_version"] = 1
    elif case == "call_limit":
        first = raw["steps"][0]
        raw["steps"] = [
            {**first, "instance_id": f"inspect_{i}"} for i in range(1, 5)
        ] + raw["steps"][1:]
    else:
        raw["final_outputs"] = {"inspect": "inspect_1"}
    with pytest.raises(ValueError):
        validate(MethodPlan.model_validate(raw))


@pytest.mark.parametrize(
    "change",
    [
        {"method_text": "invent a method"},
        {"output_schema": {}},
        {"acceptance": []},
        {"reserve": "999"},
        {"instance_id": "__method_plan__"},
    ],
)
def test_model_cannot_supply_author_or_server_fields(change):
    raw = proposal().model_dump(mode="json")
    raw["steps"][0].update(change)
    with pytest.raises(ValidationError):
        MethodPlan.model_validate(raw)


def test_instance_ids_are_unique_and_output_versions_are_strict():
    raw = proposal().model_dump(mode="json")
    raw["steps"][1]["instance_id"] = "inspect_1"
    with pytest.raises(ValidationError):
        MethodPlan.model_validate(raw)
    raw = proposal().model_dump(mode="json")
    raw["steps"][1]["inputs"][0]["version"] = True
    with pytest.raises(ValidationError):
        MethodPlan.model_validate(raw)


def test_replan_freezes_executed_prefix_and_pins_exact_accepted_version():
    raw = proposal(parent=1, review=True).model_dump(mode="json")
    context = dict(
        previous=previous(),
        protected_prefix=1,
        completed_prefix=1,
        results={"inspect_1": 7},
    )
    with pytest.raises(ValueError, match="pin"):
        validate(MethodPlan.model_validate(raw), **context)
    for step in raw["steps"][1:]:
        step["inputs"][0]["version"] = 7
    validate(MethodPlan.model_validate(raw), **context)
    forged = copy.deepcopy(raw)
    forged["steps"][1]["inputs"][0]["version"] = 6
    with pytest.raises(ValueError, match="pin"):
        validate(MethodPlan.model_validate(forged), **context)
    raw["steps"][0]["tools"] = []
    with pytest.raises(ValueError, match="prefix"):
        validate(MethodPlan.model_validate(raw), **context)


def test_started_but_not_completed_instance_is_also_protected():
    raw = proposal(parent=1).model_dump(mode="json")
    raw["steps"][0]["reason"] = "replace started instance"
    with pytest.raises(ValueError, match="prefix"):
        validate(
            MethodPlan.model_validate(raw), previous=previous(), protected_prefix=1
        )


@pytest.mark.parametrize(
    "change", [None, "tools", "source", "version", "completed", "future"]
)
def test_rework_only_rebinds_invalidated_pin_to_same_accepted_upstream(change):
    old = proposal().model_dump(mode="json")
    old["steps"][1]["inputs"][0]["version"] = 1
    receipt = previous().model_copy(update={"proposal": MethodPlan.model_validate(old)})
    new = copy.deepcopy(old)
    new["parent_version"] = 1
    new["steps"][1]["inputs"][0]["version"] = 2
    context = dict(
        previous=receipt,
        protected_prefix=2,
        completed_prefix=1,
        results={"inspect_1": 2},
    )
    if change == "tools":
        new["steps"][1]["tools"] = ["read_input"]
    elif change == "source":
        new["steps"][1]["inputs"][0]["instance_id"] = "other"
    elif change == "version":
        new["steps"][1]["inputs"][0]["version"] = 3
    elif change == "completed":
        context["completed_prefix"] = 2
    elif change == "future":
        context["completed_prefix"] = 0
    if change:
        with pytest.raises(ValueError, match="prefix"):
            validate(MethodPlan.model_validate(new), **context)
    else:
        validate(MethodPlan.model_validate(new), **context)
        assert receipt.proposal.steps[1].inputs[0].version == 1


def test_platform_capability_can_only_narrow_the_author_tool_set():
    with pytest.raises(ValueError, match="tools"):
        validate(proposal(), available_tools=frozenset())


@pytest.mark.parametrize(
    "limit,value,plan",
    [("max_steps", 2, proposal(review=True)), ("max_replans", 0, proposal(parent=1))],
)
def test_author_step_and_replan_limits(limit, value, plan):
    doc = catalog().model_dump(mode="json")
    doc["planning"][limit] = value
    with pytest.raises(ValueError, match="limit"):
        validate_plan(
            type(catalog()).model_validate(doc),
            plan,
            previous=previous() if plan.parent_version else None,
            protected_prefix=0,
            completed_prefix=0,
            results={},
            available_tools=V2_TOOLS,
        )


@pytest.mark.parametrize(
    "paths",
    [
        ("report.md", "report.md"),
        ("Report.md", "report.md"),
        ("reports", "reports/final.md"),
        ("analysis/report.md", "forecast/report.md"),
        ("analysis/Report.MD", "forecast/report.md"),
    ],
)
def test_selected_methods_cannot_overwrite_each_others_files(paths):
    doc = catalog().model_dump(mode="json")
    for unit, path in zip(doc["method_units"][:2], paths, strict=True):
        unit["contract"]["output_file"] = path
    with pytest.raises(ValueError, match="conflicting output"):
        validate_plan(
            type(catalog()).model_validate(doc),
            proposal(),
            previous=None,
            protected_prefix=0,
            completed_prefix=0,
            results={},
            available_tools=V2_TOOLS,
        )


@pytest.mark.parametrize("path", ["交回.md", "reports/交回.md", "reports/交回.MD"])
def test_method_deliverable_cannot_overwrite_the_run_handback(path):
    doc = catalog().model_dump(mode="json")
    doc["method_units"][0]["contract"]["output_file"] = path
    with pytest.raises(ValueError, match="conflicting output"):
        validate_plan(
            type(catalog()).model_validate(doc),
            proposal(),
            previous=None,
            protected_prefix=0,
            completed_prefix=0,
            results={},
            available_tools=V2_TOOLS,
        )


def test_distinct_deliverables_keep_their_author_paths_and_placement():
    from app.domain.workbench.deliverables import run_files

    doc = catalog().model_dump(mode="json")
    paths = ("analysis/inspection.md", "forecast/report.md")
    for unit, path in zip(doc["method_units"][:2], paths, strict=True):
        unit["contract"]["output_file"] = path
    author = type(catalog()).model_validate(doc)
    validate_plan(
        author,
        proposal(),
        previous=None,
        protected_prefix=0,
        completed_prefix=0,
        results={},
        available_tools=V2_TOOLS,
    )
    assert [u.contract.output_file for u in author.method_units[:2]] == list(paths)
    assert run_files(paths, "run") == [
        "run/inspection.md",
        "run/report.md",
        "run/交回.md",
    ]


@pytest.mark.parametrize("select_optional", [False, True])
def test_file_conflicts_only_apply_to_methods_selected_for_this_task(select_optional):
    doc = catalog().model_dump(mode="json")
    for unit in doc["method_units"]:
        if unit["contract"]["step_id"] in {"review", "report"}:
            unit["contract"]["output_file"] = unit["contract"]["step_id"] + "/result.md"
    author = type(catalog()).model_validate(doc)

    def check():
        validate_plan(
            author,
            proposal(review=select_optional),
            previous=None,
            protected_prefix=0,
            completed_prefix=0,
            results={},
            available_tools=V2_TOOLS,
        )

    if select_optional:
        with pytest.raises(ValueError, match="conflicting output"):
            check()
    else:
        check()


def test_final_output_cannot_reuse_an_early_success_when_later_instance_is_planned():
    doc = catalog().model_dump(mode="json")
    doc["method_units"][0]["selection"]["max_calls"] = 2
    author = type(catalog()).model_validate(doc)
    raw = proposal().model_dump(mode="json")
    raw["steps"].append({**raw["steps"][1], "instance_id": "report_2"})
    with pytest.raises(ValueError, match="last selected"):
        validate_plan(
            author,
            MethodPlan.model_validate(raw),
            previous=None,
            protected_prefix=0,
            completed_prefix=0,
            results={},
            available_tools=V2_TOOLS,
        )
