"""Build a request-local root from a reviewed selection of qualified immutable methods."""

from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from app.domain.workbench.formal_method_compiler_generated import FrozenMethod
from app.domain.workbench.formal_method_generated import (
    FormalMethodDefinition,
    MethodRef,
    Strict,
    Text,
)
from app.domain.workbench.incubation_selection import (
    Selection,
    check_proposal,
    expand,
    normalize_unresolved,
    review_issue,
)


class Choice(Strict):
    outcome: Literal["plan", "clarify", "gap"]
    steps: list[MethodRef] = Field(max_length=3)
    explanation: Text
    search_goal: str | None = Field(
        default=None, min_length=1, max_length=500, pattern=r"\S"
    )

    @model_validator(mode="after")
    def distinct_steps(self):
        if bool(self.steps) != (self.outcome == "plan") or len(set(self.steps)) != len(
            self.steps
        ):
            raise ValueError("A plan must select distinct method versions")
        if self.search_goal and self.outcome != "clarify":
            raise ValueError("Only an unresolved plan can request another search")
        return self


def root_for(task_id, question, choice, definitions):
    """Connect a producer only to a uniquely identified, compatible consumer slot."""
    if choice.outcome != "plan" or set(choice.steps) != set(definitions):
        raise ValueError("Planning needs all selected versions")
    if (
        len(choice.steps) == 1
        and definitions[choice.steps[0]].procedure.kind == "orchestration"
    ):
        return choice.steps[0], definitions[choice.steps[0]]
    steps, permissions, approvals = [], {}, []
    outputs = {}
    for n, ref in enumerate(choice.steps, 1):
        definition = definitions[ref]
        step_id = f"s{n}"
        views = definition.interfaces()
        inputs, dependencies = {}, set()
        for name, item in views["inputs"].items():
            slot = item["slot"]
            matches = [
                (step, output)
                for (step, output), previous in outputs.items()
                if slot["meaning"]
                and previous["always_present"]
                and previous["slot"]["schema"] == slot["schema"]
                and previous["slot"]["meaning"] == slot["meaning"]
            ]
            if len(matches) > 1:
                raise ValueError(f"Ambiguous source for {step_id}.{name}")
            if matches:
                step, output = matches[0]
                dependencies.add(step)
                binding = {"source": "step", "step": step, "output": output}
            else:
                binding = {"source": "caller", "name": name}
            inputs[name] = {"slot": slot, "binding": binding}
        steps.append(
            {
                "id": step_id,
                "candidates": [ref.model_dump(mode="json")],
                "select_when": "always",
                "required_when": "always",
                "always_required": True,
                "depends_on": sorted(dependencies),
                "inputs": inputs,
                "outputs": {k: v["slot"] for k, v in views["outputs"].items()},
                "on_failure": "fail",
            }
        )
        outputs.update({(step_id, k): v for k, v in views["outputs"].items()})
        for req in definition.access_requirements:
            if (
                req.requirement_id in permissions
                and permissions[req.requirement_id] != req
            ):
                raise ValueError("Conflicting permission contracts")
            permissions[req.requirement_id] = req
        for rule in definition.what_requires_approval:
            if rule not in approvals:
                approvals.append(rule)
    needed = {steps[-1]["id"]}
    for step in reversed(steps):
        if step["id"] not in needed:
            raise ValueError("A proposed step does not contribute to delivery")
        needed.update(step["depends_on"])
    last = definitions[choice.steps[-1]]
    doc = last.document()
    doc["procedure"] = {
        "kind": "orchestration",
        "instructions": choice.explanation,
        "steps": steps,
        "max_depth": 16,
        "max_steps": 100,
        "max_expansions": 1000,
        "max_replans": 0,
        "exports": {
            name: {
                "step": steps[-1]["id"],
                "output": name,
                "always_present": value["always_present"],
            }
            for name, value in last.interfaces()["outputs"].items()
        },
    }
    doc["access_requirements"] = [
        r.model_dump(mode="json") for r in permissions.values()
    ]
    doc["what_requires_approval"] = [r.model_dump(mode="json") for r in approvals]
    root = FormalMethodDefinition.model_validate(doc)
    return MethodRef(
        method_id=uuid5(UUID(str(task_id)), "temporary-plan"),
        version=1,
        digest=root.digest(),
    ), root


class PlanProposal(Selection):
    """The same business graph as incubation; a consumer may request more catalog evidence."""

    search_goal: str | None = Field(
        default=None, min_length=1, max_length=500, pattern=r"\S"
    )

    @model_validator(mode="after")
    def search_only_when_unresolved(self):
        if self.search_goal and self.status != "clarify":
            raise ValueError("Only a clarification can request another search")
        return self


class GraphChoice(Choice):
    steps: list[MethodRef] = Field(max_length=48)
    selection: Selection
    aliases: dict[str, MethodRef] = Field(max_length=48)


def read_choice(task, artifact, *, check_graph=False):
    if str(task["profile_version"]) != "7":
        return Choice.model_validate(artifact)
    proposal = PlanProposal.model_validate(normalize_unresolved(artifact))
    selection = Selection.model_validate(proposal.model_dump(exclude={"search_goal"}))
    source = task["original_input"]["input"]
    catalog = source["catalog"]["entries"]
    if check_graph:
        check_proposal(
            selection,
            {e["alias"]: e for e in catalog},
            {},
            source["runtime"]["patterns"],
        )
    pins = {e["alias"]: MethodRef.model_validate(e["reference"]) for e in catalog}
    aliases = list(
        dict.fromkeys(a for s in selection.steps for a in (s.method, *s.cases.values()))
    )
    if any(a not in pins for a in aliases):
        raise ValueError("Planner selected outside the authorized catalog")
    return GraphChoice(
        outcome="plan" if selection.status == "ready" else selection.status,
        steps=list(dict.fromkeys(pins[a] for a in aliases)),
        aliases={a: pins[a] for a in aliases},
        selection=selection,
        explanation=selection.reason,
        search_goal=proposal.search_goal,
    )


def expected_routes(selection):
    return {step.id: set(step.cases) for step in selection.steps if step.cases}


def build_selection(task_id, question, choice, definitions, runtime, cases):
    if set(choice.steps) != set(definitions) or set(choice.aliases.values()) != set(
        definitions
    ):
        raise ValueError("selection_candidate")
    proposal = choice.selection
    if issue := review_issue(proposal, runtime["patterns"]):
        raise ValueError(issue)
    if cases != expected_routes(proposal):
        raise ValueError("selection_route")
    # A complete, qualified expert may already implement routing or isolation internally.
    if len(proposal.steps) == 1:
        step = proposal.steps[0]
        pin = choice.aliases[step.method]
        original = definitions[pin]
        if original.procedure.kind == "orchestration" and not (
            step.reviews or step.cases or step.route_input or proposal.object_batch
        ):
            if step.inputs and (
                set(step.inputs) != set(original.interfaces()["inputs"])
                or any(
                    b.source != "caller" or b.name != k for k, b in step.inputs.items()
                )
            ):
                raise ValueError("selection_input")
            return pin, original
    tools = {
        a: FrozenMethod(pin, definitions[pin]) for a, pin in choice.aliases.items()
    }
    draft, roles = expand(
        proposal, tools, None, runtime["patterns"], required_cases=cases
    )
    definition = draft.definition
    doc = definition.document()
    doc["capability_declaration"]["purpose"] = question
    if roles:
        doc["procedure"]["collaboration"] = roles
    definition = FormalMethodDefinition.model_validate(doc)
    ref = MethodRef(
        method_id=uuid5(UUID(str(task_id)), "temporary-plan"),
        version=1,
        digest=definition.digest(),
    )
    return ref, definition
