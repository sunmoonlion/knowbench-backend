"""Build a request-local root from a reviewed selection of qualified immutable methods."""

from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from app.domain.workbench.formal_method_generated import (
    FormalMethodDefinition,
    MethodRef,
    Strict,
    Text,
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
