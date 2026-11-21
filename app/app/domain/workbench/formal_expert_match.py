"""Evidence for matching one question to published, exact expert declarations."""

from typing import Literal

from pydantic import Field, model_validator

from app.domain.workbench.expert_match import Requirement, Text
from app.domain.workbench.formal_method_generated import MethodRef, Strict


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
        if not mandatory <= covered <= ids or len(covered) != len(pick.coverage):
            raise ValueError(
                "Each required delivery needs its own declaration evidence"
            )
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
