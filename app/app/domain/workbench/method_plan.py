"""A task-specific linear composition of frozen author methods (WP-10).

No method text, checks, permissions or budget can be supplied by the planner.
Results passed to validation are trusted, accepted artifact versions from storage.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Set
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.domain.workbench.method_catalog import METHOD_ID, MethodCatalog
from app.domain.workbench.packs import Strict

PLAN_ARTIFACT = "__method_plan__"
PLAN_STEP = "__method_plan__"
Identifier = Annotated[str, Field(pattern=METHOD_ID)]


def output_name(instance_id: str) -> str:
    return "__method_output__:" + instance_id


class InputBinding(Strict):
    slot: Identifier
    instance_id: Identifier
    # None = output of an earlier planned step not yet executed. Bind on dispatch.
    version: int | None = Field(default=None, ge=1, strict=True)


class PlannedStep(Strict):
    instance_id: Identifier
    # Expert-local catalog key. The pinned expert resolves it to an author method
    # version; the task planner must never select a registry version itself.
    method_id: Identifier
    inputs: tuple[InputBinding, ...] = Field(default=(), max_length=30)
    tools: tuple[str, ...] = Field(default=(), max_length=30)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _unique(self) -> PlannedStep:
        if len({i.slot for i in self.inputs}) != len(self.inputs):
            raise ValueError("input slots must be unique")
        if len(set(self.tools)) != len(self.tools) or not self.reason.strip():
            raise ValueError("tools must be unique and selection reason nonblank")
        return self


class MethodPlan(Strict):
    contract: Literal["knowbench/method-plan/v1"]
    parent_version: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1, max_length=4000)
    steps: tuple[PlannedStep, ...] = Field(min_length=1, max_length=100)
    final_outputs: dict[Identifier, Identifier] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _unique(self) -> MethodPlan:
        if not self.reason.strip():
            raise ValueError("planning reason must not be blank")
        if len({s.instance_id for s in self.steps}) != len(self.steps):
            raise ValueError("instance ids must be unique")
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


class AcceptedMethodPlan(Strict):
    """Server-owned receipt; not part of the planner output schema."""

    task_id: str
    attempt_id: str
    pack_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: int = Field(ge=1, strict=True)
    proposal: MethodPlan


def validate_plan(
    catalog: MethodCatalog,
    proposal: MethodPlan,
    *,
    previous: AcceptedMethodPlan | None,
    protected_prefix: int,
    completed_prefix: int,
    results: Mapping[str, int],
    available_tools: Set[str],
) -> None:
    """Reject an invalid composition without rewriting any author or task state."""
    parent = previous.version if previous else 0
    if proposal.parent_version != parent:
        raise ValueError("plan parent is stale")
    if parent > catalog.planning.max_replans:
        raise ValueError("replanning limit exceeded")
    if len(proposal.steps) > catalog.planning.max_steps:
        raise ValueError("step limit exceeded")
    if not 0 <= completed_prefix <= protected_prefix <= len(proposal.steps):
        raise ValueError("invalid executed prefix")
    if previous is None and protected_prefix:
        raise ValueError("initial plan cannot have executed steps")
    if previous is not None:
        if protected_prefix > len(previous.proposal.steps):
            raise ValueError("invalid previous executed prefix")
        if (
            proposal.steps[:protected_prefix]
            != previous.proposal.steps[:protected_prefix]
        ):
            raise ValueError("executed prefix cannot be replaced")
    units = {u.contract.step_id: u for u in catalog.method_units}
    counts = Counter(s.method_id for s in proposal.steps)
    required = set(catalog.planning.final_units) | {
        key for key, unit in units.items() if unit.selection.required
    }
    if not required <= counts.keys():
        raise ValueError("required methods are missing")
    if set(proposal.final_outputs) != set(catalog.planning.final_units):
        raise ValueError("final outputs differ from the author contract")
    earlier: dict[str, PlannedStep] = {}
    output_paths: list[tuple[str, ...]] = []
    completed = {s.instance_id for s in proposal.steps[:completed_prefix]}
    last_call: dict[str, str] = {}
    for index, step in enumerate(proposal.steps):
        unit = units.get(step.method_id)
        if unit is None:
            raise ValueError("unknown method")
        if counts[step.method_id] > unit.selection.max_calls:
            raise ValueError("method call limit exceeded")
        if not set(step.tools) <= set(unit.contract.tools) & available_tools:
            raise ValueError("tools exceed author or platform permissions")
        if unit.contract.output_file is not None:
            # Plans can run on case-insensitive client filesystems. Two instances
            # must not overwrite a shared file or use a file as a directory.
            path = tuple(unit.contract.output_file.casefold().split("/"))
            if any(
                path[: len(other)] == other or other[: len(path)] == path
                for other in output_paths
            ):
                raise ValueError("method instances have conflicting output files")
            output_paths.append(path)
        if {i.slot for i in step.inputs} != set(unit.contract.input_refs):
            raise ValueError("method inputs do not match the author slots")
        for binding in step.inputs:
            source = earlier.get(binding.instance_id)
            if source is None or source.method_id != binding.slot:
                raise ValueError("input must reference an earlier matching method")
            if index >= protected_prefix:
                if binding.instance_id in completed:
                    if (
                        binding.version is None
                        or results.get(binding.instance_id) != binding.version
                    ):
                        raise ValueError(
                            "input must pin the current accepted artifact version"
                        )
                elif binding.version is not None:
                    raise ValueError("a future output cannot claim an artifact version")
        earlier[step.instance_id] = step
        last_call[step.method_id] = step.instance_id
    if any(
        last_call[key] != instance for key, instance in proposal.final_outputs.items()
    ):
        raise ValueError(
            "final outputs must name the last selected instance of each final method"
        )
