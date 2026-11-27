"""Server-owned selection receipt carried into the existing task ledger.

The planner may select a capability for each accepted requirement, but cannot
rewrite the requirement list or use a browser-supplied coverage claim.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from app.domain.workbench.expert_match import (
    Digest,
    Identifier,
    Requirement,
    digest,
)
from app.domain.workbench.packs import Strict


class ManualRequirement(Requirement):
    id: Literal["request"]
    kind: Literal["result"]
    required: Literal[True]
    statement: str = Field(min_length=1, max_length=4000)
    question_quote: str = Field(min_length=1, max_length=4000)


class TaskRequirements(Strict):
    question_digest: Digest
    expert_digest: Digest
    question_revision: int = Field(ge=1, strict=True)
    selection_basis: Literal["manual"]
    match_task_id: None
    match_artifact_version: None
    requirements: tuple[Requirement | ManualRequirement, ...] = Field(
        min_length=1, max_length=100
    )
    capability_mapping: dict[Identifier, tuple[Identifier, ...]] = Field(
        min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def _coverage(self) -> Self:
        ids = {req.id for req in self.requirements if req.required}
        if len({req.id for req in self.requirements}) != len(self.requirements):
            raise ValueError("task requirements must be unique")
        if self.capability_mapping.keys() != ids or any(
            not caps or len(caps) != len(set(caps))
            for caps in self.capability_mapping.values()
        ):
            raise ValueError(
                "every necessary requirement needs its exact allowed capabilities"
            )
        return self

    def digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def verify(self, *, question: str, expert_digest: str) -> None:
        if (
            self.question_digest != digest(question)
            or self.expert_digest != expert_digest
        ):
            raise ValueError("task requirements refer to another question or expert")
        if any(req.question_quote not in question for req in self.requirements):
            raise ValueError("task requirement is not grounded in this question")


def from_manual(
    *, question: str, expert_digest: str, capabilities: tuple[str, ...]
) -> TaskRequirements:
    """Keep the user's whole request; choosing an expert is not a coverage proof."""
    return TaskRequirements(
        question_digest=digest(question),
        expert_digest=expert_digest,
        question_revision=1,
        match_task_id=None,
        match_artifact_version=None,
        selection_basis="manual",
        requirements=(
            ManualRequirement(
                id="request",
                kind="result",
                statement=question,
                question_quote=question,
                required=True,
            ),
        ),
        capability_mapping={"request": capabilities},
    )
