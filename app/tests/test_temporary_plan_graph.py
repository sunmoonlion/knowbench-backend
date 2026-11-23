"""Exercise the consumer's projection of the author's A/B routing regression."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_formal_orchestration_admission import shape

from app.application.workbench.temporary_planning import compile_choice
from app.domain.workbench.formal_method_generated import (
    FormalMethodDefinition,
    MethodRef,
)
from app.domain.workbench.formal_method_plan_generated import (
    UnmatchedCandidate,
    select_plan,
)
from app.domain.workbench.planning_capabilities import host_capabilities
from app.domain.workbench.temporary_plan import GraphChoice, read_choice

QUESTION = "先提取标准明细，再按原单上的 A/B 标识分别交给费用岗或税务岗。"


def directory():
    _, _, _, dependencies = shape()
    leaf = next(iter(dependencies.values())).draft.definition
    methods = {}
    entries = []
    for alias, title in (
        ("first", "提取标准明细"),
        ("a", "费用核对"),
        ("b", "税务核对"),
    ):
        doc = leaf.document()
        doc["capability_declaration"]["title"] = title
        proc = doc["procedure"]
        slot = proc["outputs"]["receipt"]
        proc["inputs"] = {"value": slot}
        if alias != "first":
            proc["inputs"]["kind"] = slot
        definition = FormalMethodDefinition.model_validate(doc)
        ref = MethodRef(method_id=uuid4(), version=1, digest=definition.digest())
        entries.append({"alias": alias, "reference": ref.model_dump(mode="json")})
        methods[ref] = definition
    return {
        "profile_version": "7",
        "original_input": {"input": {"catalog": {"entries": entries}}},
    }, methods


def proposal():
    return {
        "status": "ready",
        "reason": QUESTION,
        "routing_required": True,
        "patterns": ["P1", "P5"],
        "steps": [
            {
                "id": "prepare",
                "method": "first",
                "task": "提取标准明细",
                "inputs": {"value": {"source": "caller", "name": "rows"}},
            },
            {
                "id": "dispatch",
                "method": "a",
                "task": "按原单标识分派",
                "inputs": {
                    "value": {"source": "step", "step": "prepare", "output": "receipt"},
                    "kind": {"source": "caller", "name": "label"},
                },
                "route_input": "kind",
                "cases": {"A": "a", "B": "b"},
            },
        ],
    }


async def test_common_prefix_and_mutually_exclusive_routes_are_compiled_and_reauthorized():
    task, methods = directory()
    choice = read_choice(task, proposal())
    assert isinstance(choice, GraphChoice)

    async def permitted(owner, pins):
        return {ref: methods[ref] for ref in pins if ref in methods}

    service = SimpleNamespace(
        formal_registry=SimpleNamespace(planning_definitions=permitted)
    )
    args = (
        service,
        "caller",
        str(uuid4()),
        QUESTION,
        choice,
        host_capabilities(True, True, True),
        ["sequence", "conditional"],
    )
    with pytest.raises(ValueError, match="selection_route"):
        await compile_choice(*args, review=SimpleNamespace(cases={"dispatch": ["A"]}))
    _, definition = await compile_choice(*args)
    for label, alias in (("A", "a"), ("B", "b")):
        fixed = select_plan(definition, {"label": label})
        first, routed = fixed.procedure.steps
        assert first.candidates == (choice.aliases["first"],)
        assert routed.depends_on == ("prepare",)
        assert routed.candidates == (choice.aliases[alias],)
    with pytest.raises(UnmatchedCandidate):
        select_plan(definition, {"label": "C"})
    del methods[choice.aliases["b"]]
    with pytest.raises(ValueError, match="selection_candidate"):
        await compile_choice(*args)


def test_unresolved_choice_uses_no_steps_and_legacy_receipt_keeps_its_original_shape():
    task, methods = directory()
    incomplete = read_choice(
        task, {**proposal(), "status": "clarify", "reason": "阈值由谁决定？"}
    )
    assert (
        incomplete.outcome == "clarify"
        and not incomplete.selection.patterns
        and not incomplete.steps
    )
    task["original_input"]["input"]["catalog"]["entries"].pop()
    with pytest.raises(ValueError, match="outside"):
        read_choice(task, proposal())
    old = {
        "outcome": "plan",
        "steps": [next(iter(methods)).model_dump(mode="json")],
        "explanation": "旧委托",
    }
    assert (
        read_choice({"profile_version": "6"}, old).model_dump(
            mode="json", exclude_none=True
        )
        == old
    )


async def test_immediate_planning_rejects_an_unexecutable_secondary_route_at_preview():
    task, methods = directory()
    branch = task["original_input"]["input"]["catalog"]["entries"][-1]
    old = MethodRef.model_validate(branch["reference"])
    doc = methods.pop(old).document()
    proc = doc["procedure"]
    proc["implementation"].update(
        kind="registered_instruction", source=str(uuid4()), entry="instruction"
    )
    proc["max_model_turns"] = 2
    invalid = FormalMethodDefinition.model_validate(doc)
    pin = old.model_copy(update={"digest": invalid.digest()})
    methods[pin] = invalid
    branch["reference"] = pin.model_dump(mode="json")
    choice = read_choice(task, proposal())

    async def permitted(owner, pins):
        return {ref: methods[ref] for ref in pins}

    service = SimpleNamespace(
        formal_registry=SimpleNamespace(planning_definitions=permitted)
    )
    with pytest.raises(ValueError, match="Unsupported execution contract"):
        await compile_choice(
            service,
            "caller",
            str(uuid4()),
            QUESTION,
            choice,
            host_capabilities(True, True, True),
            ["conditional"],
        )
