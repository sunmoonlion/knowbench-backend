"""Formal task snapshots and deterministic evaluation; no legacy pack translation.

Expected answers belong to the test runner, never this execution snapshot or a
tool's arguments. The snapshot grants no permission and carries no credentials.
"""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_method_admission_generated import (
    FormalMethodDraft,
    FormalMethodSource,
    FrozenFormalMethod,
)
from app.domain.workbench.formal_method_execution_generated import ModelProbeLimits
from app.domain.workbench.formal_method_generated import (
    MethodRef,
    Orchestration,
    Strict,
    ToolProcedure,
)

FORMAL_RUNTIME = "knowlab/formal-method-execution/v1"
FORMAL_SOURCE = "formal-test"


class IncubationBinding(Strict):
    reference: MethodRef
    listing_id: UUID
    source: FormalMethodSource


def same_implementation(original, local):
    # An installed Skill's source URI contains the account-local asset ID.
    return (
        original.kind == local.kind
        and original.kind in ("mcp", "skill_script")
        and original.version == local.version
        and original.digest == local.digest
        and original.entry == local.entry
        and local.asset_id is not None
        and (
            original.source == local.source
            if original.kind == "mcp"
            else local.source == "skill:" + str(local.asset_id)
        )
    )


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
    dependencies: tuple[FrozenFormalMethod, ...] = Field(
        default=(), max_length=1000, exclude_if=lambda value: not value
    )
    bindings: tuple[IncubationBinding, ...] = Field(
        default=(), max_length=1000, exclude_if=lambda value: not value
    )
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
        dependencies = {
            (f.reference.method_id, f.reference.version): f for f in self.dependencies
        }
        seen = set()
        for binding in self.bindings:
            original = dependencies.get(
                (binding.reference.method_id, binding.reference.version)
            )
            if (
                not original
                or original.reference != binding.reference
                or original.visibility != "public"
                or original.draft.source is None
                or binding.reference in seen
                or not same_implementation(original.draft.source, binding.source)
                or binding.source.license_basis == "pending"
            ):
                raise ValueError("Invalid cross-author execution binding")
            seen.add(binding.reference)
        if self.definition_digest != self.draft.definition.digest():
            raise ValueError("Formal definition digest changed")
        if self.caller not in self.draft.allowed_callers:
            raise ValueError("The candidate does not allow this caller")
        if self.caller == "model":
            if self.model_prompt is None or self.model_probe is None:
                raise ValueError("Model cases require a bounded caller probe")
        elif self.model_prompt is not None or self.model_probe is not None:
            raise ValueError("Host cases cannot contain model probe inputs")
        if isinstance(self.draft.definition.procedure, Orchestration):
            orchestration_plan(self)
        elif self.dependencies:
            raise ValueError("Direct methods cannot contain dependency snapshots")
        return self

    def execution_source(self, frozen):
        return next(
            (b.source for b in self.bindings if b.reference == frozen.reference),
            frozen.draft.source,
        )

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


def recursive_plan(snapshot: FormalTaskSnapshot) -> bool:
    return any(
        isinstance(f.draft.definition.procedure, Orchestration)
        for f in snapshot.dependencies
    )


def expanded_plan(snapshot: FormalTaskSnapshot):
    from app.domain.workbench.formal_method_compiler_generated import FrozenMethod
    from app.domain.workbench.formal_method_plan_generated import expand_unique_plan

    return expand_unique_plan(
        snapshot.method_id,
        snapshot.draft.definition,
        {
            (f.reference.method_id, f.reference.version): FrozenMethod(
                f.reference, f.draft.definition
            )
            for f in snapshot.dependencies
        },
    )


def orchestration_plan(snapshot: FormalTaskSnapshot):
    """Validate the complete pinned closure; execution expands only reached nodes."""
    from app.domain.workbench.formal_method_plan_generated import unique_plan

    if snapshot.caller != "host" or snapshot.draft.allowed_callers != ("host",):
        raise ValueError("Formal orchestration currently supports the host caller only")
    refs = [
        (f.reference.method_id, f.reference.version, f.reference.digest)
        for f in snapshot.dependencies
    ]
    if len(set(refs)) != len(refs):
        raise ValueError("Formal orchestration requires unique frozen dependency pins")
    for frozen in snapshot.dependencies:
        if "host" not in frozen.draft.allowed_callers:
            raise ValueError("Formal dependency must allow the host caller")
        if isinstance(frozen.draft.definition.procedure, ToolProcedure):
            if frozen.draft.source and frozen.draft.source.kind == "skill_instruction":
                if (
                    recursive_plan(snapshot)
                    or frozen.visibility != "private"
                    or frozen.draft.allowed_callers != ("host",)
                ):
                    raise ValueError(
                        "Instruction leaves require an owned one-level host plan"
                    )
            else:
                direct_procedure(frozen.draft, caller="host")
        elif frozen.draft.allowed_callers != ("host",):
            raise ValueError("Formal orchestration dependencies must be host-only")
    plan = expanded_plan(snapshot)
    wanted = {
        (n.reference.method_id, n.reference.version, n.reference.digest)
        for n in plan.nodes
    }
    if set(refs) != wanted:
        raise ValueError(
            "Formal orchestration requires the exact frozen dependency closure"
        )
    return unique_plan(snapshot.draft.definition)


def execution_steps(snapshot: FormalTaskSnapshot):
    return tuple(node.step for node in expanded_plan(snapshot).leaf_nodes)


def execution_slot(snapshot: FormalTaskSnapshot, index: int) -> str:
    node = expanded_plan(snapshot).leaf_nodes[index]
    return (
        "node:" + digest(list(node.path)) if recursive_plan(snapshot) else node.step.id
    )


def dependency_for_step(snapshot: FormalTaskSnapshot, step):
    return next(f for f in snapshot.dependencies if f.reference == step.candidates[0])


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


def direct_tool_procedure(draft: FormalMethodDraft) -> ToolProcedure:
    procedure = draft.definition.procedure
    if (
        not isinstance(procedure, ToolProcedure)
        or procedure.implementation.kind not in ("mcp", "skill_script")
        or procedure.max_model_turns != 0
    ):
        raise ValueError(
            "Execution requires a zero-turn MCP or isolated script implementation"
        )
    return procedure


def direct_procedure(
    draft: FormalMethodDraft, *, caller: str = "host"
) -> ToolProcedure:
    procedure = direct_tool_procedure(draft)
    if procedure.implementation.kind == "skill_script" and (
        caller != "host" or draft.allowed_callers != ("host",)
    ):
        raise ValueError("An isolated script method requires host execution")
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
        elif not slot.accepts(values[name]):
            raise ValueError(f"Method value does not match its schema: {name}")
    return values


def structured_outputs(draft: FormalMethodDraft, result: dict) -> dict:
    procedure = direct_tool_procedure(draft)
    if result.get("isError"):
        raise ValueError("The method implementation reported an execution error")
    if procedure.implementation.kind == "skill_script":
        from app.domain.workbench.skill_execution import method_script_outputs

        return validate_values(procedure.outputs, method_script_outputs(result))
    return validate_values(procedure.outputs, result.get("structuredContent"))


def case_key(case_id: str, repetition: int) -> str:
    return f"{case_id}:{repetition}"
