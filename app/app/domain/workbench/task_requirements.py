"""Server-owned selection receipt carried into the existing task ledger.

The planner may select a capability for each accepted requirement, but cannot
rewrite the requirement list or use a browser-supplied coverage claim.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from app.domain.workbench.expert_match import (
    AnyMatchReceipt,
    Digest,
    ExpertMatchV3,
    Identifier,
    MethodProposal,
    MethodRequirement,
    Requirement,
    Solution,
    digest,
    requirement_capabilities,
)
from app.domain.workbench.packs import Strict


class TaskRequirements(Strict):
    question_digest: Digest
    expert_digest: Digest
    question_revision: int = Field(ge=1, strict=True)
    match_task_id: str = Field(min_length=1, max_length=128)
    match_artifact_version: int = Field(ge=1, strict=True)
    requirements: tuple[MethodRequirement | Requirement, ...] = Field(
        min_length=1, max_length=100
    )
    capability_mapping: dict[Identifier, tuple[Identifier, ...]] = Field(
        min_length=1, max_length=100
    )

    method_proposals: tuple[MethodProposal, ...] = Field(
        default=(), max_length=30, exclude_if=lambda value: not value
    )
    solution: Solution | None = Field(
        default=None, exclude_if=lambda value: value is None
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
        proposals = {p.id: p for p in self.method_proposals}
        all_ids = {r.id for r in self.requirements}
        if len(proposals) != len(self.method_proposals) or proposals.keys() & all_ids:
            raise ValueError("duplicate or conflicting method proposal ids")
        if any(not set(p.requirement_ids) <= all_ids for p in proposals.values()):
            raise ValueError("unknown proposal requirement")
        if (
            self.solution
            and not set(self.solution.method_proposal_ids) <= proposals.keys()
        ):
            raise ValueError("solution refers to an unknown proposal")
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


def from_match(
    receipt: AnyMatchReceipt,
    *,
    match_task_id: str,
    match_artifact_version: int,
    knowbot_id: str,
    version: str,
    expert_digest: str,
    question: str,
) -> TaskRequirements:
    mapping = requirement_capabilities(
        receipt,
        knowbot_id=knowbot_id,
        version=version,
        pack_digest=expert_digest,
        question=question,
    )
    return TaskRequirements(
        question_digest=receipt.question_digest,
        expert_digest=expert_digest,
        question_revision=receipt.question_revision,
        match_task_id=match_task_id,
        match_artifact_version=match_artifact_version,
        requirements=receipt.result.requirements,
        capability_mapping={key: tuple(sorted(caps)) for key, caps in mapping.items()},
        **(
            {
                "method_proposals": receipt.result.method_proposals,
                "solution": next(
                    p.solution
                    for p in receipt.result.picks
                    if (p.knowbot_id, p.version) == (knowbot_id, version)
                ),
            }
            if isinstance(receipt.result, ExpertMatchV3)
            else {}
        ),
    )
