"""Wire types pinned to the KnowLab incubation contract."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.domain.workbench.formal_method_generated import (
    CapabilityDeclaration,
    InputSource,
    MethodRef,
    Name,
    ObjectBatch,
    Strict,
    ValueSlot,
)

Pattern = Literal["P1", "P2", "P3", "P4", "P5"]
Short = Annotated[str, Field(min_length=1, max_length=1500, pattern=r"\S")]


class SketchStep(Strict):
    id: Name
    task: Short
    method: Name
    inputs: dict[Name, InputSource] = Field(default_factory=dict, max_length=100)
    # Cases map caller values to versioned catalog aliases.
    route_input: Name | None = None
    cases: dict[Short, Name] = Field(default_factory=dict, max_length=8)
    reviews: tuple[Name, ...] = Field(default=(), max_length=8)


class Selection(Strict):
    status: Literal["ready", "clarify", "gap"]
    reason: Short
    patterns: tuple[Pattern, ...] = Field(max_length=5)
    steps: tuple[SketchStep, ...] = Field(max_length=16)
    routing_required: bool = Field(strict=True)
    object_batch: ObjectBatch | None = None

    @model_validator(mode="after")
    def check_status(self):
        if self.status != "ready" and (
            self.steps or self.patterns or self.object_batch
        ):
            raise ValueError(
                "Unresolved choices belong in the reason, not the proposed graph"
            )
        if self.status == "ready" and not self.steps:
            raise ValueError("A ready sketch needs an available method")
        if len(set(self.patterns)) != len(self.patterns):
            raise ValueError("Duplicate pattern")
        return self


class InstructionPin(Strict):
    asset_id: str | None = Field(default=None, exclude_if=lambda v: v is None)
    asset_revision: int | None = Field(
        default=None, ge=1, exclude_if=lambda v: v is None
    )
    kind: Literal["registered_instruction", "skill_instruction"]
    source: str
    entry: str
    digest: str
    version: str


class SelectionCandidate(Strict):
    isolated_instruction: bool = False
    installation: str | None = Field(default=None, exclude_if=lambda v: v is None)
    scope: Literal["owned", "shared"]
    reference: MethodRef
    declaration: CapabilityDeclaration
    inputs: dict[Name, ValueSlot]
    outputs: dict[Name, ValueSlot]


class SelectionContext(Strict):
    instructions: dict[Name, InstructionPin] = Field(
        default_factory=dict, max_length=24, exclude_if=lambda v: not v
    )
    methods: dict[Name, SelectionCandidate] = Field(max_length=48)
    patterns: dict[Name, bool]
    partial: bool
    previous: Selection | None = Field(default=None, exclude_if=lambda v: v is None)
