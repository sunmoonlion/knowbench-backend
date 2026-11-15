"""Formal task snapshots and deterministic evaluation; no legacy pack translation.

Expected answers belong to the test runner, never this execution snapshot or a
tool's arguments. The snapshot grants no permission and carries no credentials.
"""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_method_admission_generated import FormalMethodDraft
from app.domain.workbench.formal_method_execution_generated import ModelProbeLimits
from app.domain.workbench.formal_method_generated import Strict, ToolProcedure

FORMAL_RUNTIME = "knowlab/formal-method-execution/v1"
FORMAL_SOURCE = "formal-test"


class FormalTaskSnapshot(Strict):
    runtime_contract: Literal["knowlab/formal-method-execution/v1"]
    run_id: UUID
    case_id: str = Field(min_length=1, max_length=64)
    repetition: int = Field(strict=True, ge=1, le=10)
    request_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    method_id: UUID
    draft: FormalMethodDraft
    arguments: dict[str, Any]
    definition_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    caller: Literal["host", "model"] = Field(
        default="host", exclude_if=lambda value: value == "host"
    )
    model_prompt: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
        exclude_if=lambda value: value is None,
    )
    model_probe: ModelProbeLimits | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def pinned(self):
        if self.definition_digest != self.draft.definition.digest():
            raise ValueError("Formal definition digest changed")
        if self.caller not in self.draft.allowed_callers:
            raise ValueError("The candidate does not allow this caller")
        if self.caller == "model":
            if self.model_prompt is None or self.model_probe is None:
                raise ValueError("Model cases require a bounded caller probe")
        elif self.model_prompt is not None or self.model_probe is not None:
            raise ValueError("Host cases cannot contain model probe inputs")
        return self

    def document(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


def is_formal(task: dict) -> bool:
    return task.get("pack_source") == FORMAL_SOURCE or (
        isinstance(task.get("pack_snapshot"), dict)
        and task["pack_snapshot"].get("runtime_contract") == FORMAL_RUNTIME
    )


def is_formal_model(task: dict) -> bool:
    return (
        is_formal(task) and (task.get("pack_snapshot") or {}).get("caller") == "model"
    )


def is_formal_host(task: dict) -> bool:
    return is_formal(task) and not is_formal_model(task)


def snapshot_for_task(task: dict) -> FormalTaskSnapshot:
    if task.get("pack_source") != FORMAL_SOURCE:
        raise ValueError("Formal execution requires its own candidate identity")
    snapshot = FormalTaskSnapshot.model_validate(task.get("pack_snapshot"))
    if task.get("pack_digest") != digest(snapshot.document()):
        raise ValueError("Formal task snapshot changed")
    return snapshot


@dataclass(frozen=True)
class PinnedFormalMethod:
    snapshot: FormalTaskSnapshot
    version: str

    def task_columns(self) -> dict:
        document = self.snapshot.document()
        return {
            "pack_source": FORMAL_SOURCE,
            "pack_digest": digest(document),
            "pack_snapshot": document,
        }


def direct_mcp_procedure(draft: FormalMethodDraft) -> ToolProcedure:
    procedure = draft.definition.procedure
    if (
        not isinstance(procedure, ToolProcedure)
        or procedure.implementation.kind != "mcp"
        or procedure.max_model_turns != 0
    ):
        raise ValueError("Execution requires a zero-turn direct MCP implementation")
    return procedure


def direct_procedure(
    draft: FormalMethodDraft, *, caller: str = "host"
) -> ToolProcedure:
    procedure = direct_mcp_procedure(draft)
    if caller not in {"host", "model"} or caller not in draft.allowed_callers:
        raise ValueError("Formal method does not allow this caller")
    return procedure


def validate_values(slots: dict, values: Any) -> dict:
    if not isinstance(values, dict) or set(values) - set(slots):
        raise ValueError("Values must name only declared method slots")
    for name, slot in slots.items():
        if name not in values:
            if slot.required or slot.on_missing != "omit":
                raise ValueError(f"Missing method value: {name}")
        elif not Draft202012Validator(slot.schema_).is_valid(values[name]):
            raise ValueError(f"Method value does not match its schema: {name}")
    return values


def structured_outputs(draft: FormalMethodDraft, result: dict) -> dict:
    if result.get("isError"):
        raise ValueError("MCP reported an execution error")
    return validate_values(
        direct_mcp_procedure(draft).outputs, result.get("structuredContent")
    )


def case_key(case_id: str, repetition: int) -> str:
    return f"{case_id}:{repetition}"
