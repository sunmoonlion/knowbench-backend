"""Evidence for matching one question to published, exact expert declarations."""

from graphlib import TopologicalSorter
from typing import Literal

from pydantic import Field, model_validator

from app.domain.workbench.expert_match import Requirement, Text
from app.domain.workbench.formal_method_generated import (
    FormalMethodDefinition,
    MethodRef,
    Orchestration,
    Strict,
)


class PromiseCoverage(Strict):
    requirement: str
    promise: str
    quote: Text


class ExpertOption(Strict):
    reference: MethodRef
    reason: Text
    coverage: tuple[PromiseCoverage, ...] = Field(min_length=1, max_length=30)
    limitations: tuple[Text, ...] = Field(max_length=12)


class FormalExpertMatch(Strict):
    contract: Literal["knowbench/expert-match/v5"]
    requirements: tuple[Requirement, ...] = Field(min_length=1, max_length=30)
    outcome: Literal["candidates", "clarify", "no_match"]
    picks: tuple[ExpertOption, ...] = Field(max_length=3)
    explanation: Text
    clarification: Text | None

    @model_validator(mode="after")
    def outcome_shape(self):
        if (self.outcome == "clarify") != bool(
            self.clarification and self.clarification.strip()
        ):
            raise ValueError("Clarification requires one question")
        if (self.outcome == "candidates") != bool(self.picks):
            raise ValueError("Only suitable candidates can be offered for selection")
        return self


def verify_options(question, directory, content):
    match = FormalExpertMatch.model_validate(content)
    mandatory = {r.id for r in match.requirements if r.required}
    ids = {r.id for r in match.requirements}
    if (
        not mandatory
        or len(ids) != len(match.requirements)
        or any(r.question_quote not in question for r in match.requirements)
    ):
        raise ValueError(
            "Requirements must be distinct and grounded in the current question"
        )
    seen = set()
    for pick in match.picks:
        ref = pick.reference.model_dump(mode="json")
        entry = next((e for e in directory["items"] if e["reference"] == ref), None)
        if entry is None or pick.reference in seen:
            raise ValueError("Candidate missing, repeated or version changed")
        seen.add(pick.reference)
        covered = {c.requirement for c in pick.coverage}
        missing = sorted(mandatory - covered)
        if missing:
            raise ValueError(
                f"Missing coverage for required delivery or constraint IDs: {', '.join(missing)}. "
                "Cite one published promise per required ID, including input constraints, "
                "or remove this unsupported candidate."
            )
        if not covered <= ids or len(covered) != len(pick.coverage):
            raise ValueError("Use distinct declared requirement IDs in coverage")
        for item in pick.coverage:
            promise = entry["declaration"]["promises"].get(item.promise)
            if (
                not promise
                or not item.quote.strip()
                or item.quote not in promise
                or not entry["promise_outputs"].get(item.promise)
            ):
                raise ValueError(
                    "Evidence must quote a published promise with validated outputs"
                )
    return match


def assess_proposal(question, directory, content):
    """Hold a grounded recommendation for clarification before offering any choice."""
    if (
        isinstance(content, dict)
        and content.get("outcome") == "candidates"
        and isinstance(content.get("clarification"), str)
        and content["clarification"].strip()
    ):
        verify_options(question, directory, {**content, "clarification": None})
        content = {**content, "outcome": "clarify", "picks": []}
    return verify_options(question, directory, content)


def frozen_scope(definition: FormalMethodDefinition):
    """Project the root contract for its buyer without opening private child definitions."""
    graph = definition.procedure
    if not isinstance(graph, Orchestration):
        raise ValueError("Expected an expert orchestration")
    steps = {step.id: step for step in graph.steps}
    order = TopologicalSorter(
        {key: sorted(steps[key].depends_on) for key in sorted(steps)}
    )
    interfaces = definition.interfaces()
    return {
        "steps": [
            {
                "id": key,
                "depends_on": steps[key].depends_on,
                "required_when": None
                if steps[key].always_required
                else steps[key].required_when,
                "candidate_count": len(steps[key].candidates),
            }
            for key in order.static_order()
        ],
        "inputs": [
            {
                "name": name,
                "description": value["slot"]["description"],
                "required_by": value["required_by"],
                "used_by": value["used_by"],
            }
            for name, value in interfaces["inputs"].items()
        ],
        "outputs": [
            {
                "name": name,
                "description": value["slot"]["description"],
                "step": graph.exports[name].step,
            }
            for name, value in interfaces["outputs"].items()
        ],
        "requirements": [
            {
                "id": req.requirement_id,
                "capability": req.capability_ref,
                "version": req.version,
                "operations": req.operations,
                "resource_roles": [r.resource_role for r in req.resources],
                "destination": req.data_transfer.destination,
                "fields": req.data_transfer.argument_fields,
                "purpose": req.data_transfer.purpose,
            }
            for req in definition.access_requirements
        ],
        "approvals": [
            {
                "requirement_id": rule.requirement_id,
                "operations": rule.operations,
                "when": rule.when,
            }
            for rule in definition.what_requires_approval
        ],
    }
