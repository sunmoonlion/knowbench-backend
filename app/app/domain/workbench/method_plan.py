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

from pydantic import Field, TypeAdapter, model_validator

from app.domain.workbench.deliverables import HANDBACK_FILE, placed
from app.domain.workbench.method_catalog import (
    METHOD_ID,
    CapabilityMethodCatalog,
    MethodCatalog,
    SlottedMethodContract,
)
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


class SlottedPlannedStep(PlannedStep):
    omitted_inputs: dict[Identifier, Literal["not_selected"]] = Field(max_length=30)


class CapabilityMethodPlan(MethodPlan):
    contract: Literal["knowbench/method-plan/v2"]
    steps: tuple[SlottedPlannedStep, ...] = Field(min_length=1, max_length=100)
    requirement_coverage: dict[Identifier, Identifier] = Field(
        min_length=1, max_length=100
    )
    unselected_methods: dict[Identifier, str] = Field(max_length=30)

    @model_validator(mode="after")
    def _reasons(self) -> CapabilityMethodPlan:
        if any(
            not why.strip() or len(why) > 2000
            for why in self.unselected_methods.values()
        ):
            raise ValueError("unselected methods require bounded nonblank reasons")
        return self


AnyMethodPlan = MethodPlan | CapabilityMethodPlan
_PLAN = TypeAdapter(AnyMethodPlan)


def parse_plan(document: object) -> AnyMethodPlan:
    return _PLAN.validate_python(document)


class AcceptedMethodPlan(Strict):
    """Server-owned receipt; not part of the planner output schema."""

    task_id: str
    attempt_id: str
    pack_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    version: int = Field(ge=1, strict=True)
    proposal: AnyMethodPlan


def validate_plan(
    catalog: MethodCatalog,
    proposal: AnyMethodPlan,
    *,
    previous: AcceptedMethodPlan | None,
    protected_prefix: int,
    completed_prefix: int,
    results: Mapping[str, int],
    available_tools: Set[str],
    requirement_capabilities: Mapping[str, Set[str]] | None = None,
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
        if proposal.contract != previous.proposal.contract:
            raise ValueError("an accepted plan cannot change contract versions")
        if protected_prefix > len(previous.proposal.steps):
            raise ValueError("invalid previous executed prefix")
        for index in range(protected_prefix):
            before = previous.proposal.steps[index]
            after = proposal.steps[index]
            if before == after:
                continue
            # Rework may invalidate a started consumer's explicit input pin.
            # Only that version may change, in a new receipt; method identity,
            # ordering, dependencies, tools and the completed prefix stay frozen.
            if (
                index < completed_prefix
                or before.model_copy(update={"inputs": after.inputs}) != after
                or len(before.inputs) != len(after.inputs)
            ):
                raise ValueError("executed prefix cannot be replaced")
            for old, new in zip(before.inputs, after.inputs, strict=True):
                if old == new:
                    continue
                if (
                    old.model_copy(update={"version": new.version}) != new
                    or old.version is None
                    or new.version is None
                    or results.get(new.instance_id) != new.version
                    or new.instance_id
                    not in {s.instance_id for s in proposal.steps[:completed_prefix]}
                ):
                    raise ValueError(
                        "executed prefix input must pin its accepted replacement"
                    )
    validate_composition(catalog, proposal, available_tools=available_tools)
    if isinstance(proposal, CapabilityMethodPlan):
        # These are the accepted request/matching requirements supplied by the
        # application, not requirements newly invented by the plan itself.
        if requirement_capabilities is None or set(
            proposal.requirement_coverage
        ) != set(requirement_capabilities):
            raise ValueError("plan must cover the authoritative task requirements")
        if any(
            capability not in requirement_capabilities[key]
            for key, capability in proposal.requirement_coverage.items()
        ):
            raise ValueError(
                "plan capability does not satisfy the accepted requirement"
            )
    completed = {s.instance_id for s in proposal.steps[:completed_prefix]}
    for step in proposal.steps[protected_prefix:]:
        for binding in step.inputs:
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


def validate_composition(
    catalog: MethodCatalog, proposal: AnyMethodPlan, *, available_tools: Set[str]
) -> None:
    """Validate frozen structure independently of the current execution cursor.

    Loading an accepted historical plan must not treat its already-bound inputs
    as future outputs. Temporal checks belong to acceptance and dispatch.
    """
    slotted = isinstance(catalog, CapabilityMethodCatalog)
    if slotted != isinstance(proposal, CapabilityMethodPlan):
        raise ValueError("plan contract does not match the author catalog")
    if proposal.parent_version > catalog.planning.max_replans:
        raise ValueError("replanning limit exceeded")
    if len(proposal.steps) > catalog.planning.max_steps:
        raise ValueError("step limit exceeded")
    units = {u.contract.step_id: u for u in catalog.method_units}
    counts = Counter(s.method_id for s in proposal.steps)
    required = set(catalog.planning.final_units) | {
        key for key, unit in units.items() if unit.selection.required
    }
    if isinstance(catalog, CapabilityMethodCatalog) and isinstance(
        proposal, CapabilityMethodPlan
    ):
        for capability in proposal.requirement_coverage.values():
            binding = catalog.planning.capability_bindings.get(capability)
            if binding is None:
                raise ValueError("plan names an unknown author capability")
            required.update(binding.required_units)
        if set(proposal.unselected_methods) != units.keys() - counts.keys():
            raise ValueError("explain every unselected author method exactly once")
    if not required <= counts.keys():
        raise ValueError("required methods are missing")
    if set(proposal.final_outputs) != set(catalog.planning.final_units):
        raise ValueError("final outputs differ from the author contract")
    earlier: dict[str, PlannedStep] = {}
    output_paths: list[tuple[str, ...]] = []
    # All deliverables share this run's folder, and placed() retains only their
    # basename. Validate the actual destinations as well as the declared paths;
    # different author directories must not hide an overwrite or the handback.
    destinations = {placed(HANDBACK_FILE, "run").casefold()}
    last_call: dict[str, str] = {}
    for step in proposal.steps:
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
            destination = placed(unit.contract.output_file, "run").casefold()
            if destination in destinations or any(
                path[: len(other)] == other or other[: len(path)] == path
                for other in output_paths
            ):
                raise ValueError("method instances have conflicting output files")
            output_paths.append(path)
            destinations.add(destination)
        if isinstance(step, SlottedPlannedStep) and isinstance(
            unit.contract, SlottedMethodContract
        ):
            bound = {i.slot for i in step.inputs}
            omitted = set(step.omitted_inputs)
            if bound & omitted or bound | omitted != set(unit.contract.input_refs):
                raise ValueError("bind or explicitly omit every author input slot")
            for slot in omitted:
                author_input = unit.contract.input_slots[slot]
                if author_input.required:
                    raise ValueError("a required input cannot be omitted")
                if author_input.method_id in counts:
                    raise ValueError("a selected upstream cannot be omitted")
        elif {i.slot for i in step.inputs} != set(unit.contract.input_refs):
            raise ValueError("method inputs do not match the author slots")
        for binding in step.inputs:
            source = earlier.get(binding.instance_id)
            expected = (
                unit.contract.input_slots[binding.slot].method_id
                if isinstance(unit.contract, SlottedMethodContract)
                else binding.slot
            )
            if source is None or source.method_id != expected:
                raise ValueError("input must reference an earlier matching method")
        earlier[step.instance_id] = step
        last_call[step.method_id] = step.instance_id
    if any(
        last_call[key] != instance for key, instance in proposal.final_outputs.items()
    ):
        raise ValueError(
            "final outputs must name the last selected instance of each final method"
        )
    if slotted:
        # Every selected result must reach a declared final output through an
        # explicit binding. Neither conversation history nor an empty optional
        # slot can stand in for the analysis the task actually executed.
        by_instance = {step.instance_id: step for step in proposal.steps}
        reachable = set(proposal.final_outputs.values())
        pending = list(reachable)
        while pending:
            for binding in by_instance[pending.pop()].inputs:
                if binding.instance_id not in reachable:
                    reachable.add(binding.instance_id)
                    pending.append(binding.instance_id)
        if reachable != by_instance.keys():
            raise ValueError("selected results must reach a final deliverable")
