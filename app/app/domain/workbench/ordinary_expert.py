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
)

ORDINARY_SOURCE = "formal-expert"
CONFIRMED = "task/formal_expert_execution_confirmed"


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
    policies: dict[str, str]
    caller: Literal["host"] = "host"

    @model_validator(mode="after")
    def verify_plan(self):
        from app.domain.workbench.formal_execution import expanded_plan
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
            or any(len(n.path) != 1 or n.kind != "tool" for n in graph.nodes)
            or any(
                m.draft.source is None
                or m.draft.source.kind != "mcp"
                or m.draft.source.license_basis == "pending"
                for m in self.dependencies
            )
            or set(self.policies) != {n.step.id for n in graph.nodes}
        ):
            raise ValueError("Unsupported ordinary execution snapshot")
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
