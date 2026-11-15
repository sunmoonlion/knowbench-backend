"""Authenticated session use of an exact, privately admitted method version.

This boundary neither discovers tools for a model nor grants market, composition,
derivation or delivery rights. Expected test answers have no place in a request.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from app.domain.workbench.capability_contract import Call
from app.domain.workbench.formal_method_admission_generated import UncoveredScenario
from app.domain.workbench.formal_method_execution_generated import bounded_json
from app.domain.workbench.formal_method_generated import MethodRef, Strict, ValueSlot
from app.domain.workbench.method_capabilities import AccessRequirement


class FormalMethodCallRequest(Strict):
    request_id: UUID
    reference: MethodRef
    values: dict[str, JsonValue] = Field(max_length=100)

    @model_validator(mode="after")
    def finite(self):
        bounded_json(self.values)
        return self


class AvailableFormalMethod(Strict):
    reference: MethodRef
    title: str = Field(min_length=1, max_length=20000)
    purpose: str = Field(min_length=1, max_length=20000)
    inputs: dict[str, ValueSlot]
    outputs: dict[str, ValueSlot]
    access_requirements: tuple[AccessRequirement, ...]
    uncovered: tuple[UncoveredScenario, ...]
    available: bool
    issue: str | None = Field(max_length=2000)


class FormalMethodCatalogResponse(Strict):
    items: list[AvailableFormalMethod]


class FormalMethodInvocation(Strict):
    request_id: UUID
    method_call_id: UUID
    reference: MethodRef
    title: str = Field(min_length=1, max_length=20000)
    status: Literal[
        "awaiting_approval",
        "dispatching",
        "succeeded",
        "failed",
        "declined",
        "unknown",
        "expired",
    ]
    call: Call
    outputs: dict[str, JsonValue] | None
    detail: str = Field(max_length=2000)

    @model_validator(mode="after")
    def outcome(self):
        if self.call.detail.execution_kind not in (
            "formal_method",
            "formal_method_model",
        ):
            raise ValueError("Formal invocation requires its own dispatch kind")
        if self.call.detail.execution_kind == "formal_method_model" and (
            self.call.detail.reference != self.reference
            or self.call.detail.method_title != self.title
        ):
            raise ValueError(
                "Model method result must identify its exact frozen version"
            )
        if self.method_call_id == self.call.id:
            raise ValueError("Method identity and implementation call are distinct")
        if (self.status == "succeeded") != (self.outputs is not None):
            raise ValueError("Only a typed successful method has outputs")
        if self.status != self.call.status and not (
            self.status == "failed" and self.call.status == "succeeded"
        ):
            raise ValueError("Method result must be projected from its actual dispatch")
        if self.outputs is not None:
            bounded_json(self.outputs)
        return self


class FormalMethodInvocations(Strict):
    calls: list[FormalMethodInvocation] = Field(max_length=50)
