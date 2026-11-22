"""The consumer's confirmed fixed plan, pinned to an ordinary match and local sources."""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_method_admission_generated import FormalMethodDraft
from app.domain.workbench.formal_method_generated import (
    Digest,
    MethodRef,
    Orchestration,
    Strict,
    ToolProcedure,
)

ORDINARY_SOURCE = "formal-expert"
CONFIRMED = "task/formal_expert_execution_confirmed"


class MaterialPin(Strict):
    material_id: UUID
    version_no: int = Field(strict=True, gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ConsumerMethod(Strict):
    reference: MethodRef
    draft: FormalMethodDraft


class ConsumerPlan(Strict):
    runtime_contract: Literal["knowbench/formal-expert/v1"] = (
        "knowbench/formal-expert/v1"
    )
    match_task_id: UUID
    preparation_revision: int = Field(gt=0)
    preview_digest: Digest
    reference: MethodRef
    draft: FormalMethodDraft
    arguments: dict[str, Any]
    dependencies: tuple[ConsumerMethod, ...]
    materials: dict[str, MaterialPin] = Field(
        default_factory=dict, exclude_if=lambda pins: not pins
    )
    scope_hours: int | None = Field(
        default=None, strict=True, gt=0, exclude_if=lambda value: value is None
    )
    policies: dict[str, str]
    caller: Literal["host"] = "host"

    @model_validator(mode="after")
    def verify_plan(self):
        from app.domain.workbench.formal_execution import expanded_plan, plan_slot
        from app.domain.workbench.formal_method_plan_generated import (
            validate_plan_inputs,
        )

        if (
            not isinstance(self.draft.definition.procedure, Orchestration)
            or self.draft.definition.digest() != self.reference.digest
        ):
            raise ValueError("Consumer root digest changed")
        graph = expanded_plan(self)
        if (
            self.draft.allowed_callers != ("host",)
            or self.draft.definition.procedure.collaboration
            or any(
                method.reference not in graph.dependencies
                for method in self.dependencies
            )
            or len(graph.dependencies) != len(self.dependencies)
            or any(
                m.draft.allowed_callers != ("host",)
                or (
                    isinstance(m.draft.definition.procedure, Orchestration)
                    and m.draft.definition.procedure.collaboration
                )
                or (
                    m.draft.source is not None
                    and (
                        m.draft.source.kind
                        not in (
                            "mcp",
                            "service_function",
                            "skill_script",
                            "registered_script",
                            "skill_instruction",
                            "registered_instruction",
                        )
                        or m.draft.source.license_basis == "pending"
                    )
                )
                for m in self.dependencies
            )
            or (self.scope_hours is not None) != (graph.depth > 2)
            or set(self.policies)
            != {plan_slot(n.path, nested=graph.depth > 2) for n in graph.leaf_nodes}
        ):
            raise ValueError("Unsupported ordinary execution snapshot")
        reads = set()
        for leaf in graph.leaf_nodes:
            procedure = leaf.definition.procedure
            if not isinstance(procedure, ToolProcedure):
                raise ValueError("Expected a fixed tool step")
            impl = procedure.implementation
            if impl.kind != "service_function":
                continue
            if (impl.source, impl.entry) != ("knowbase", "read_method_input"):
                raise ValueError("Unsupported ordinary service function")
            reads.add(plan_slot(leaf.path, nested=graph.depth > 2))
        if self.materials.keys() != reads:
            raise ValueError("Material selections must match the frozen read steps")
        validate_plan_inputs(self.draft.definition, self.arguments)
        return self

    @property
    def method_id(self):
        return self.reference.method_id

    @property
    def version(self):
        return str(self.reference.version)

    @property
    def definition_digest(self):
        return self.reference.digest

    def execution_source(self, method):
        return method.draft.source

    def identity(self):
        return {
            "match_task_id": str(self.match_task_id),
            "preparation_revision": self.preparation_revision,
            "preview_digest": self.preview_digest,
        }

    def task_columns(self):
        document = self.model_dump(mode="json", by_alias=True)
        return {
            "pack_source": ORDINARY_SOURCE,
            "pack_digest": digest(document),
            "pack_snapshot": document,
        }
