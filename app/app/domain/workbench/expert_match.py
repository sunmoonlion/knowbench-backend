"""Question-grounded expert matching against public author declarations only.

These checks establish identity, completeness and provenance, not semantic
truth. Whether a statement really covers a question still needs match evals.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from app.domain.workbench.method_catalog import PublicCapability
from app.domain.workbench.packs import Scope, Strict

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


class Requirement(Strict):
    id: Identifier
    kind: Literal["result", "constraint", "explicit_method"]
    statement: Text
    question_quote: Text
    required: bool = Field(strict=True)

    @model_validator(mode="after")
    def _nonblank(self) -> Self:
        if any(not value.strip() for value in (self.statement, self.question_quote)):
            raise ValueError(
                "requirements need nonblank statements and question quotes"
            )
        if self.kind == "explicit_method" and not self.required:
            raise ValueError("explicitly requested methods cannot become optional")
        return self


class PublicExpert(Strict):
    knowbot_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    digest: Digest
    name: str = Field(min_length=1, max_length=80)
    tagline: str = Field(max_length=200)
    scope: Scope
    declaration: Literal["scope/v1", "capabilities/v1"]
    runtime_contract: Literal[
        "knowbench/fixed/v1",
        "knowbench/dynamic-methods/v1",
        "knowbench/dynamic-methods/v2",
        "knowbench/dynamic-methods/v3",
    ]
    public_capabilities: tuple[PublicCapability, ...] = Field(max_length=30)
    free: bool = Field(strict=True)
    price: str | None = Field(pattern=r"^[0-9]+(\.[0-9]+)?$", max_length=32)

    @model_validator(mode="after")
    def _declaration(self) -> Self:
        ids = {cap.id for cap in self.public_capabilities}
        if len(ids) != len(self.public_capabilities):
            raise ValueError("duplicate public capability")
        if (self.declaration == "capabilities/v1") != bool(ids):
            raise ValueError("declaration format differs from its public capabilities")
        if self.runtime_contract.endswith("/v3") and not ids:
            raise ValueError("capability runtime requires public declarations")
        return self


class DirectorySnapshot(Strict):
    contract: Literal["knowmart/expert-directory/v2"]
    complete: bool = Field(strict=True)
    experts: tuple[PublicExpert, ...] = Field(max_length=1000)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        keys = [(entry.knowbot_id, entry.version) for entry in self.experts]
        if len(set(keys)) != len(keys):
            raise ValueError("directory versions must be unique")
        return self


class Coverage(Strict):
    requirement_id: Identifier
    status: Literal["covered", "uncovered", "unknown"]
    capability_id: Identifier | None
    declaration_quote: str = Field(max_length=2000)
    reason: Text


class MatchedExpert(Strict):
    knowbot_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    reason: Text
    coverage: tuple[Coverage, ...] = Field(min_length=1, max_length=100)
    unresolved_conditions: tuple[Text, ...] = Field(max_length=30)


class Recommendation(Strict):
    action: Literal["direct", "incubate"]
    reason: Text


class DemandDraft(Strict):
    title: str = Field(min_length=1, max_length=120)
    problem: str = Field(min_length=1, max_length=4000)


class ExpertMatch(Strict):
    contract: Literal["knowbench/expert-match/v2"]
    requirements: tuple[Requirement, ...] = Field(min_length=1, max_length=100)
    outcome: Literal["summon", "candidates", "none", "clarify"]
    picks: tuple[MatchedExpert, ...] = Field(max_length=3)
    clarification: Text | None
    recommend: Recommendation | None
    demand: DemandDraft | None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        ids = {req.id for req in self.requirements}
        if len(ids) != len(self.requirements) or not any(
            r.required for r in self.requirements
        ):
            raise ValueError(
                "requirements must be unique and include a necessary result"
            )
        if len({(pick.knowbot_id, pick.version) for pick in self.picks}) != len(
            self.picks
        ):
            raise ValueError("expert picks must be unique")
        for pick in self.picks:
            covered = [row.requirement_id for row in pick.coverage]
            if len(set(covered)) != len(covered) or set(covered) != ids:
                raise ValueError(
                    "each pick must account for every question requirement"
                )
        if self.outcome == "summon" and len(self.picks) != 1:
            raise ValueError("summon needs exactly one expert")
        if self.outcome == "candidates" and not self.picks:
            raise ValueError("candidates needs at least one expert")
        if (self.outcome == "clarify") != bool(
            self.clarification and self.clarification.strip()
        ):
            raise ValueError("only clarify has a nonblank clarification")
        if self.outcome == "none" and self.recommend is None:
            raise ValueError("no match needs a next-action recommendation")
        return self


class MatchReceipt(Strict):
    contract: Literal["knowbench/expert-match-receipt/v2"]
    question_digest: Digest
    directory_digest: Digest
    question_revision: int = Field(ge=1, strict=True)
    result: ExpertMatch
    # The public directory snapshot is durable evidence; no private methods.
    directory: DirectorySnapshot


class MethodRequirement(Requirement):
    """A necessary capability is distinct from a proposed way to satisfy it."""

    origin: Literal["user_explicit", "goal_inferred"]
    rationale: Text

    @model_validator(mode="after")
    def _origin(self) -> Self:
        if not self.rationale.strip():
            raise ValueError("requirement rationale cannot be blank")
        if self.kind == "explicit_method" and (
            self.origin != "user_explicit" or not self.required
        ):
            raise ValueError("an explicitly requested method must remain mandatory")
        return self


class MethodProposal(Strict):
    id: Identifier
    requirement_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    approach: Text
    reason: Text
    data_requirements: tuple[Text, ...] = Field(max_length=30)

    @model_validator(mode="after")
    def _proposal(self) -> Self:
        if len(set(self.requirement_ids)) != len(self.requirement_ids):
            raise ValueError("proposal requirement references must be unique")
        if not self.approach.strip() or not self.reason.strip():
            raise ValueError("method proposals need a nonblank approach and reason")
        return self


class Solution(Strict):
    """Question-specific public approach, not a private execution DAG or consent."""

    approach: Text
    method_proposal_ids: tuple[Identifier, ...] = Field(max_length=30)
    data_conditions: tuple[Text, ...] = Field(max_length=30)

    @model_validator(mode="after")
    def _solution(self) -> Self:
        if not self.approach.strip() or len(set(self.method_proposal_ids)) != len(
            self.method_proposal_ids
        ):
            raise ValueError("solution needs an approach and unique proposals")
        return self


class SolutionExpert(MatchedExpert):
    solution: Solution


class ExpertMatchV3(ExpertMatch):
    contract: Literal["knowbench/expert-match/v3"]  # type: ignore[assignment]
    requirements: tuple[MethodRequirement, ...] = Field(min_length=1, max_length=100)
    method_proposals: tuple[MethodProposal, ...] = Field(max_length=30)
    picks: tuple[SolutionExpert, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def _proposals(self) -> Self:
        requirements = {r.id for r in self.requirements}
        proposals = {p.id: p for p in self.method_proposals}
        if (
            len(proposals) != len(self.method_proposals)
            or requirements & proposals.keys()
        ):
            raise ValueError(
                "proposal ids must be unique and distinct from requirements"
            )
        if any(not set(p.requirement_ids) <= requirements for p in proposals.values()):
            raise ValueError("proposal refers to an unknown requirement")
        for pick in self.picks:
            if not set(pick.solution.method_proposal_ids) <= proposals.keys():
                raise ValueError("solution refers to an unknown method proposal")
        return self


class MatchReceiptV3(MatchReceipt):
    contract: Literal["knowbench/expert-match-receipt/v3"]  # type: ignore[assignment]
    result: ExpertMatchV3


class DeclarationEvidence(Strict):
    capability_id: Identifier | None
    quote: Text


class DeclarationPick(Strict):
    knowbot_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    reason: Text
    fit: Literal["suitable", "unsuitable", "clarify"]
    declarations: tuple[DeclarationEvidence, ...] = Field(min_length=1, max_length=30)
    limitations: tuple[Text, ...] = Field(max_length=30)


class DeclarationMatch(Strict):
    """Reasoned selection from declarations, not a proof of method coverage."""

    contract: Literal["knowbench/expert-match/v4"]
    requirements: tuple[Requirement, ...] = Field(min_length=1, max_length=100)
    outcome: Literal["summon", "candidates", "none", "clarify"]
    picks: tuple[DeclarationPick, ...] = Field(max_length=3)
    clarification: Text | None
    recommend: Recommendation | None
    demand: DemandDraft | None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if len({r.id for r in self.requirements}) != len(self.requirements):
            raise ValueError("duplicate question requirement")
        if any(
            r.kind == "explicit_method" and not r.required for r in self.requirements
        ):
            raise ValueError("explicitly requested methods cannot become optional")
        if not any(r.required for r in self.requirements):
            raise ValueError("the original request must remain required")
        if len({(p.knowbot_id, p.version) for p in self.picks}) != len(self.picks):
            raise ValueError("duplicate expert pick")
        if self.outcome == "summon" and len(self.picks) != 1:
            raise ValueError("summon needs exactly one expert")
        if self.outcome == "candidates" and not self.picks:
            raise ValueError("candidates needs at least one expert")
        if self.outcome in {"summon", "candidates"} and any(
            p.fit != "suitable" for p in self.picks
        ):
            raise ValueError("known mismatch or material ambiguity cannot invite")
        if self.outcome == "none" and (
            self.recommend is None or any(p.fit == "suitable" for p in self.picks)
        ):
            raise ValueError("no match requires a remedy and no suitable picks")
        if (self.outcome == "clarify") != bool(
            self.clarification and self.clarification.strip()
        ):
            raise ValueError("only clarify has a nonblank clarification")
        return self


class DeclarationReceipt(Strict):
    contract: Literal["knowbench/expert-match-receipt/v4"]
    question_digest: Digest
    directory_digest: Digest
    question_revision: int = Field(ge=1, strict=True)
    result: DeclarationMatch
    directory: DirectorySnapshot


def _validate_declaration_match(
    question: str, result: DeclarationMatch, directory: DirectorySnapshot, revision: int
) -> DeclarationReceipt:
    entries = {(e.knowbot_id, e.version): e for e in directory.experts}
    for pick in result.picks:
        entry = entries.get((pick.knowbot_id, pick.version))
        if entry is None:
            raise ValueError("expert version is not in this directory snapshot")
        capabilities = {c.id: c for c in entry.public_capabilities}
        for evidence in pick.declarations:
            if evidence.capability_id is None:
                texts = [entry.scope.does, entry.scope.does_not, entry.scope.elsewhere]
            else:
                cap = capabilities.get(evidence.capability_id)
                if cap is None:
                    raise ValueError("unknown public capability")
                texts = [cap.statement, *cap.conditions, *cap.exclusions, *cap.delivers]
            if not evidence.quote.strip() or not any(
                evidence.quote in text for text in texts
            ):
                raise ValueError("selection must cite an actual public declaration")
        if result.outcome == "summon" and not entry.free:
            raise ValueError("paid experts require explicit user selection")
    return DeclarationReceipt(
        contract="knowbench/expert-match-receipt/v4",
        question_digest=digest(question),
        directory_digest=digest(directory.model_dump(mode="json")),
        question_revision=revision,
        result=result,
        directory=directory,
    )


AnyExpertMatch = ExpertMatch | ExpertMatchV3 | DeclarationMatch
AnyMatchReceipt = MatchReceipt | MatchReceiptV3 | DeclarationReceipt
_matches = TypeAdapter(AnyExpertMatch)


def parse_match(document: Any) -> AnyExpertMatch:
    return _matches.validate_python(document)


class MatchSelection(Strict):
    match_task_id: str = Field(min_length=1, max_length=128)
    match_artifact_version: int = Field(ge=1, strict=True)
    question_revision: int = Field(ge=1, strict=True)
    knowbot_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")


def validate_match(
    question: str,
    result: AnyExpertMatch,
    directory: DirectorySnapshot,
    *,
    revision: int = 1,
) -> AnyMatchReceipt:
    if not directory.complete:
        raise ValueError("incomplete directory cannot establish a matching outcome")
    if not question.strip():
        raise ValueError("question cannot be blank")
    for requirement in result.requirements:
        if requirement.question_quote not in question:
            raise ValueError("requirement quote is not in the original question")
    if isinstance(result, DeclarationMatch):
        return _validate_declaration_match(question, result, directory, revision)
    entries = {(e.knowbot_id, e.version): e for e in directory.experts}
    necessary = {r.id for r in result.requirements if r.required}
    fully_covered: set[tuple[str, str]] = set()
    for pick in result.picks:
        key = (pick.knowbot_id, pick.version)
        if key not in entries:
            raise ValueError("expert version is not in this directory snapshot")
        entry = entries[key]
        capabilities = {cap.id: cap for cap in entry.public_capabilities}
        for coverage in pick.coverage:
            if coverage.capability_id is not None:
                cap = capabilities.get(coverage.capability_id)
                if cap is None:
                    raise ValueError("coverage names an unknown public capability")
                declarations = [cap.statement, *cap.conditions, *cap.delivers]
            else:
                declarations = [entry.scope.does]
            if coverage.status == "covered":
                if (
                    entry.declaration == "capabilities/v1"
                    and coverage.capability_id is None
                ):
                    raise ValueError(
                        "covered requirements must name the author's capability"
                    )
                if not coverage.declaration_quote.strip() or not any(
                    coverage.declaration_quote in text for text in declarations
                ):
                    raise ValueError(
                        "coverage must quote the matching public declaration"
                    )
        covered = {r.requirement_id for r in pick.coverage if r.status == "covered"}
        if necessary <= covered and not pick.unresolved_conditions:
            fully_covered.add(key)
        if result.outcome in {"summon", "candidates"} and key not in fully_covered:
            raise ValueError("partial or uncertain coverage cannot invite an expert")
        if result.outcome == "summon" and not entry.free:
            raise ValueError("paid experts require explicit user selection")
    if result.outcome == "none" and fully_covered:
        raise ValueError("fully covered picks cannot be reported as no match")
    receipt_type = MatchReceiptV3 if isinstance(result, ExpertMatchV3) else MatchReceipt
    return receipt_type.model_validate(
        {
            "contract": "knowbench/expert-match-receipt/v3"
            if isinstance(result, ExpertMatchV3)
            else "knowbench/expert-match-receipt/v2",
            "question_digest": digest(question),
            "directory_digest": digest(directory.model_dump(mode="json")),
            "question_revision": revision,
            "result": result.model_dump(mode="json"),
            "directory": directory.model_dump(mode="json"),
        }
    )


def verify_receipt(receipt: AnyMatchReceipt, question: str) -> None:
    rebuilt = validate_match(
        question, receipt.result, receipt.directory, revision=receipt.question_revision
    )
    if rebuilt != receipt:
        raise ValueError("matching receipt does not match the question and directory")


def requirement_capabilities(
    receipt: AnyMatchReceipt,
    *,
    knowbot_id: str,
    version: str,
    pack_digest: str,
    question: str,
) -> dict[str, set[str]]:
    """Recover coverage from trusted receipt storage, never from the task planner."""
    verify_receipt(receipt, question)
    if receipt.result.outcome not in {"summon", "candidates"}:
        raise ValueError("matching receipt does not authorize expert selection")
    entry = next(
        (
            e
            for e in receipt.directory.experts
            if (e.knowbot_id, e.version) == (knowbot_id, version)
        ),
        None,
    )
    pick = next(
        (
            p
            for p in receipt.result.picks
            if (p.knowbot_id, p.version) == (knowbot_id, version)
        ),
        None,
    )
    if entry is None or pick is None or entry.digest != pack_digest:
        raise ValueError("matching receipt pins a different expert version")
    if isinstance(receipt.result, DeclarationMatch):
        # Execution may choose from the author's declared capabilities. This is
        # an allowed range, NOT a precomputed claim that each capability fits.
        return {
            r.id: {cap.id for cap in entry.public_capabilities}
            for r in receipt.result.requirements
            if r.required
        }
    wanted = {req.id for req in receipt.result.requirements if req.required}
    assert isinstance(pick, MatchedExpert)
    answer = {
        row.requirement_id: {row.capability_id}
        for row in pick.coverage
        if row.status == "covered"
        and row.capability_id is not None
        and row.requirement_id in wanted
    }
    if answer.keys() != wanted:
        raise ValueError("task requirements have no complete capability mapping")
    return answer
