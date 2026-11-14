"""Formal-method admission contract; a session installation is not a method.

Dependency kinds in this contract must be resolved from admitted immutable
versions by the registration service. Remote descriptors cannot assert them.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_method_compiler_generated import classify_dependencies
from app.domain.workbench.formal_method_generated import MethodRef as MethodReference
from app.domain.workbench.method_capabilities import Requirement

MethodKind = Literal["tool", "expert", "expert_team"]
Nonblank = Annotated[str, Field(min_length=1, max_length=2000)]
Fingerprint = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class AdmittedDependency(Requirement):
    reference: MethodReference
    method_kind: MethodKind


def classify_method(
    *,
    implementation: Literal["direct", "orchestration"],
    dependencies: tuple[AdmittedDependency, ...],
) -> MethodKind:
    """Count distinct expert identities over ALL permitted candidates, not a run."""
    return classify_dependencies(
        implementation,
        tuple((d.reference.method_id, d.method_kind) for d in dependencies),
    )


class ValidationEvidence(Requirement):
    """Reference to author/runtime evidence, not client-provided pass booleans."""

    evidence_id: UUID
    definition_digest: Fingerprint
    checks: tuple[
        Literal[
            "definition",
            "source",
            "dependencies",
            "authorization",
            "approval",
            "isolation",
            "outcome",
            "business",
        ],
        ...,
    ] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("Duplicate admission evidence check")
        return self


class UncoveredScenario(Requirement):
    scenario: Nonblank
    reason: Nonblank
    scope: Nonblank

    @model_validator(mode="after")
    def nonblank(self):
        if any(not text.strip() for text in (self.scenario, self.reason, self.scope)):
            raise ValueError("Uncovered scenario must explain the concrete limitation")
        return self


class MethodAdmission(Requirement):
    reference: MethodReference
    implementation: Literal["direct", "orchestration"]
    dependencies: tuple[AdmittedDependency, ...] = Field(max_length=100)
    evidence: tuple[ValidationEvidence, ...] = Field(min_length=1, max_length=100)
    visibility: Literal["private", "public"]
    uncovered: tuple[UncoveredScenario, ...] = Field(max_length=30)

    @model_validator(mode="after")
    def admission(self):
        classify_method(
            implementation=self.implementation, dependencies=self.dependencies
        )
        refs = [(d.reference.method_id, d.reference.version) for d in self.dependencies]
        if len(set(refs)) != len(refs):
            raise ValueError("Duplicate method dependency version")
        if any(
            d.reference.method_id == self.reference.method_id for d in self.dependencies
        ):
            raise ValueError(
                "A method cannot depend on itself, including another version"
            )
        if any(e.definition_digest != self.reference.digest for e in self.evidence):
            raise ValueError("Admission evidence must name this exact definition")
        if len({e.evidence_id for e in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate admission evidence reference")
        checks = {c for e in self.evidence for c in e.checks}
        if (
            not {
                "definition",
                "source",
                "dependencies",
                "authorization",
                "approval",
                "isolation",
                "outcome",
            }
            <= checks
        ):
            raise ValueError("Required admission governance evidence is missing")
        if "business" not in checks and (
            self.visibility != "private" or not self.uncovered
        ):
            raise ValueError(
                "Unverified business effects require a private, explicit limitation"
            )
        if self.visibility == "public" and self.uncovered:
            raise ValueError(
                "Public admission requires its declared validation coverage"
            )
        return self

    @property
    def method_kind(self) -> MethodKind:
        return classify_method(
            implementation=self.implementation, dependencies=self.dependencies
        )

    @property
    def dispatch(self) -> Literal["direct", "orchestration"]:
        return "direct" if self.method_kind == "tool" else "orchestration"

    def fingerprint(self) -> str:
        return "sha256:" + digest(self.model_dump(mode="json"))


def validate_dependency_graph(
    root: MethodAdmission,
    resolved: dict[tuple[UUID, int], MethodAdmission],
    *,
    max_depth: int = 16,
    max_expansions: int = 1000,
) -> None:
    """Every edge must resolve to admitted evidence, including unselected branches."""
    count = 0

    def visit(method: MethodAdmission, path: tuple[UUID, ...]) -> None:
        nonlocal count
        count += 1
        mid = method.reference.method_id
        if mid in path or len(path) >= max_depth or count > max_expansions:
            raise ValueError("Method dependency cycle or expansion limit")
        for dependency in method.dependencies:
            ref = dependency.reference
            found = resolved.get((ref.method_id, ref.version))
            if (
                found is None
                or found.reference != ref
                or found.method_kind != dependency.method_kind
            ):
                raise ValueError("Dependency is not the exact admitted method version")
            visit(found, (*path, mid))

    visit(root, ())
