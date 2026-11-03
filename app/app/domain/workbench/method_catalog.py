"""Frozen author methods, not a task's execution plan (WP-10).

Input refs in a catalog contract name method output slots. A task plan must bind
each slot to a specific earlier instance/artifact version before execution.
This model deliberately has no workflow/step(index) interface.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, Self, TypeVar
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from app.domain.workbench.packs import AcceptanceRule, Scope, StepContract, Strict

RUNTIME_CONTRACT = "knowbench/dynamic-methods/v1"
METHOD_ID = r"^[A-Za-z][A-Za-z0-9_-]{0,47}$"


class MethodSelection(Strict):
    applies_when: str = Field(min_length=1, max_length=4000)
    required: bool = Field(strict=True)
    max_calls: int = Field(ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def _nonblank(self) -> MethodSelection:
        if not self.applies_when.strip():
            raise ValueError("applies_when must not be blank")
        return self


class PlanningContract(Strict):
    method_text: str = Field(min_length=1, max_length=20000)
    final_units: tuple[str, ...] = Field(min_length=1, max_length=30)
    max_steps: int = Field(ge=1, le=100, strict=True)
    max_replans: int = Field(ge=0, le=10, strict=True)
    reserve: str = Field(pattern=r"^[0-9]{1,4}(\.[0-9]{1,4})?$")
    input_max_chars: int = Field(ge=500, le=200000, strict=True)

    @model_validator(mode="after")
    def _consistent(self) -> PlanningContract:
        if not self.method_text.strip() or Decimal(self.reserve) <= 0:
            raise ValueError("planning needs author text and a positive reserve")
        if len(set(self.final_units)) != len(self.final_units):
            raise ValueError("final_units must be unique")
        return self


class MethodContract(StepContract):
    """Same execution contract, with bounded counts for repeatable catalog methods."""

    step_id: str = Field(pattern=METHOD_ID)
    output_artifact: str = Field(pattern=METHOD_ID)
    tools: tuple[str, ...] = Field(default=(), max_length=30)
    input_refs: tuple[str, ...] = Field(default=(), max_length=30)
    max_reworks: int = Field(default=1, ge=0, le=5, strict=True)
    max_backs: int = Field(default=1, ge=0, le=5, strict=True)
    input_max_chars: int = Field(default=8000, ge=500, le=200000, strict=True)


class MethodUnit(Strict):
    selection: MethodSelection
    contract: MethodContract

    @model_validator(mode="after")
    def _slot_contract(self) -> MethodUnit:
        step = self.contract
        if not re.fullmatch(METHOD_ID, step.step_id):
            raise ValueError("invalid method id")
        if step.output_artifact != step.step_id:
            raise ValueError("method output slot must equal its method id")
        if not step.title.strip() or not step.method_text.strip():
            raise ValueError("method title and text are required")
        if step.mode != "single":
            raise ValueError("dynamic methods do not enable competing attempts")
        if len(set(step.input_refs)) != len(step.input_refs):
            raise ValueError("input slots must be unique")
        if len(set(step.tools)) != len(step.tools):
            raise ValueError("tools must be unique")
        if not re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,4})?", step.reserve):
            raise ValueError("invalid method reserve")
        if Decimal(step.reserve) <= 0:
            raise ValueError("method reserve must be positive")
        kinds = {r.kind for r in step.acceptance}
        if not {"json_object", "required_keys", "no_positioning_advice"} <= kinds:
            raise ValueError("method must retain the mandatory output checks")
        if not step.output_schema or not any(
            r.kind == "required_keys" and set(step.output_schema) <= set(r.keys)
            for r in step.acceptance
        ):
            raise ValueError("all output keys must be required")
        for rule in step.acceptance:
            if rule.source and rule.source.split(".", 1)[0] not in step.input_refs:
                raise ValueError("acceptance source must name a declared input slot")
        if step.on_reject == "back":
            if step.back_to not in step.input_refs:
                raise ValueError("back_to must name a declared input slot")
        elif step.back_to is not None:
            raise ValueError("only back rejection can name back_to")
        return self


Identifier = Annotated[str, Field(pattern=METHOD_ID)]
CapabilityText = Annotated[str, Field(min_length=1, max_length=2000)]


class PublicCapability(Strict):
    """Author-confirmed public claims, never private method definitions."""

    id: Identifier
    statement: CapabilityText
    conditions: tuple[CapabilityText, ...] = Field(max_length=30)
    exclusions: tuple[CapabilityText, ...] = Field(max_length=30)
    delivers: tuple[CapabilityText, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _text(self) -> Self:
        if any(
            not value.strip()
            for value in (
                self.statement,
                *self.conditions,
                *self.exclusions,
                *self.delivers,
            )
        ):
            raise ValueError("capability text must not be blank")
        return self


class CapabilityOutput(Strict):
    method: Identifier
    field: str = Field(min_length=1, max_length=64)


class CapabilityBinding(Strict):
    required_units: tuple[Identifier, ...] = Field(min_length=1, max_length=30)
    evidence_outputs: tuple[CapabilityOutput, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.required_units)) != len(self.required_units):
            raise ValueError("capability methods must be unique")
        pairs = [(out.method, out.field) for out in self.evidence_outputs]
        if len(set(pairs)) != len(pairs):
            raise ValueError("capability outputs must be unique")
        if not {out.method for out in self.evidence_outputs} <= set(
            self.required_units
        ):
            raise ValueError("capability evidence must come from its required methods")
        return self


class CapabilityPlanning(PlanningContract):
    capability_bindings: dict[Identifier, CapabilityBinding] = Field(
        min_length=1, max_length=30
    )


class MethodInputSlot(Strict):
    method_id: Identifier
    required: bool = Field(strict=True)


class ConditionalAcceptanceRule(AcceptanceRule):
    when_input_present: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _guard(self) -> Self:
        if self.when_input_present and self.kind in (
            "json_object",
            "required_keys",
            "no_positioning_advice",
        ):
            raise ValueError("mandatory output and safety checks cannot be conditional")
        return self


class SlottedMethodContract(MethodContract):
    """v3 input_refs are neutral slots; bindings belong to the expert version."""

    input_slots: dict[Identifier, MethodInputSlot] = Field(max_length=30)
    acceptance: tuple[ConditionalAcceptanceRule, ...] = ()

    @model_validator(mode="after")
    def _slots(self) -> Self:
        if set(self.input_refs) != set(self.input_slots):
            raise ValueError("every method input must have an explicit slot binding")
        optional = {key for key, slot in self.input_slots.items() if not slot.required}
        for rule in self.acceptance:
            if rule.when_input_present and rule.when_input_present not in optional:
                raise ValueError("conditional checks must name an optional input")
            if rule.source:
                source = rule.source.split(".", 1)[0]
                if source in optional and rule.when_input_present != source:
                    raise ValueError(
                        "optional coverage requires its explicit input guard"
                    )
                if rule.when_input_present and rule.when_input_present != source:
                    raise ValueError("coverage guard must name its own source")
        if self.back_to in optional:
            raise ValueError("a back target must be a required input")
        return self


class SlottedMethodUnit(MethodUnit):
    contract: SlottedMethodContract


def input_methods(
    step: MethodContract, *, required_only: bool = False
) -> tuple[str, ...]:
    if isinstance(step, SlottedMethodContract):
        return tuple(
            step.input_slots[slot].method_id
            for slot in step.input_refs
            if not required_only or step.input_slots[slot].required
        )
    return step.input_refs


Runtime = TypeVar("Runtime", bound=str)


class _Catalog(Strict, Generic[Runtime]):
    runtime_contract: Runtime
    pack_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    title: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=80)
    tagline: str = Field(default="", max_length=200)
    scope: Scope
    planning: PlanningContract
    method_units: tuple[MethodUnit, ...] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _references(self) -> Self:
        units = {u.contract.step_id: u for u in self.method_units}
        if len(units) != len(self.method_units):
            raise ValueError("method ids must be unique")
        if not set(self.planning.final_units) <= units.keys():
            raise ValueError("final_units must name catalog methods")
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(key: str) -> None:
            if key not in units:
                raise ValueError("input slot names an unknown method")
            if key in visiting:
                raise ValueError("method inputs must not form a cycle")
            if key in visited:
                return
            visiting.add(key)
            for ref in input_methods(units[key].contract):
                visit(ref)
            visiting.remove(key)
            visited.add(key)

        required = set(self.planning.final_units) | {
            key for key, unit in units.items() if unit.selection.required
        }
        for key in units:
            visit(key)
        needed: set[str] = set()

        def require(key: str) -> None:
            if key in needed:
                return
            needed.add(key)
            for ref in input_methods(units[key].contract, required_only=True):
                require(ref)

        for key in required:
            require(key)
        if len(needed) > self.planning.max_steps:
            raise ValueError("step limit cannot cover required methods and inputs")
        return self


class DynamicMethodCatalog(_Catalog[Literal["knowbench/dynamic-methods/v1"]]):
    pass


class MethodReference(Strict):
    method_id: UUID
    version: int = Field(ge=1, le=999999, strict=True)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    inputs: dict[str, str] = Field(default_factory=dict, max_length=30)

    @model_validator(mode="after")
    def _bindings(self) -> Self:
        if any(
            not re.fullmatch(METHOD_ID, v) for pair in self.inputs.items() for v in pair
        ):
            raise ValueError("method bindings must name slots and aliases")
        if len(set(self.inputs.values())) != len(self.inputs):
            raise ValueError("method input slots cannot be merged")
        return self


class MethodSource(Strict):
    reference: MethodReference
    # Opaque author evidence. The provider owns its schema/compiler, not the
    # runtime. Pin the exact JSON, never normalize it through a second author DTO.
    definition: dict[str, Any]

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        encoded = json.dumps(
            self.definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        actual = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        if self.reference.digest != actual:
            raise ValueError("method definition digest differs")
        if self.definition.get("schema") != "knowlab/method-definition/v1":
            raise ValueError("unsupported author method definition")
        if set(self.definition) != {"schema", "step", "input_schemas"}:
            raise ValueError("invalid method definition envelope")
        if not isinstance(self.definition["step"], dict) or not isinstance(
            self.definition["input_schemas"], dict
        ):
            raise ValueError("invalid method definition body")
        return self


class ReferencedMethodCatalog(_Catalog[Literal["knowbench/dynamic-methods/v2"]]):
    method_compiler: Literal["knowlab/workdraft-compiler/v1"]
    method_sources: dict[str, MethodSource] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _pinned_methods(self) -> Self:
        units = {unit.contract.step_id: unit for unit in self.method_units}
        if not self.method_sources.keys() <= units.keys():
            raise ValueError("method source must name a catalog alias")
        for alias, source in self.method_sources.items():
            unit = units[alias]
            ref = source.reference
            original = source.definition["step"]
            schemas = source.definition["input_schemas"]
            slots = original.get("uses")
            selection = original.get("selection")
            if (
                original.get("id") != "method"
                or not isinstance(slots, list)
                or any(not isinstance(slot, str) for slot in slots)
                or not isinstance(selection, dict)
            ):
                raise ValueError("invalid independent method identity or slots")
            if (
                set(slots) != set(schemas)
                or set(slots) != set(ref.inputs)
                or len(slots) != len(set(slots))
            ):
                raise ValueError("bind every independent method input slot")
            maximum = selection.get("max_calls")
            if (
                selection.get("required") is not False
                or type(maximum) is not int
                or unit.selection.max_calls > maximum
                or unit.selection.applies_when != selection.get("applies_when")
            ):
                raise ValueError("expert selection widens the frozen method")
            if unit.contract.step_version != str(ref.version):
                raise ValueError("compiled method version differs")
            if tuple(ref.inputs[s] for s in slots) != unit.contract.input_refs:
                raise ValueError("compiled method inputs differ from slot bindings")
            for slot, upstream in ref.inputs.items():
                if upstream not in units or not isinstance(schemas[slot], dict):
                    raise ValueError("method input has no compatible upstream")
                provided = units[upstream].contract.output_schema
                if any(
                    k not in provided or provided[k] != v
                    for k, v in schemas[slot].items()
                ):
                    raise ValueError("method input schema differs from upstream output")
        return self


class SlottedMethodReference(MethodReference):
    execution: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SlottedMethodSource(MethodSource):
    reference: SlottedMethodReference

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        encoded = json.dumps(
            self.definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if (
            self.reference.digest
            != "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
        ):
            raise ValueError("method definition digest differs")
        version = self.definition.get("schema")
        if version == "knowlab/method-definition/v3":
            if set(self.definition) != {
                "schema",
                "title",
                "purpose",
                "procedure",
                "applies_when",
                "inputs",
                "rules",
                "outputs",
                "output_schema",
                "checks",
            } or not isinstance(self.definition.get("inputs"), dict):
                raise ValueError("invalid atomic method definition envelope")
            if not isinstance(self.reference.execution, dict):
                raise ValueError("atomic method needs expert execution policy")
            return self
        if self.reference.execution is not None:
            raise ValueError("legacy method execution cannot be overridden")
        if version not in (
            "knowlab/method-definition/v1",
            "knowlab/method-definition/v2",
        ):
            raise ValueError("unsupported author method definition")
        keys = {"schema", "step", "input_schemas"}
        if version.endswith("/v2"):
            keys.add("input_required")
        if set(self.definition) != keys:
            raise ValueError("invalid method definition envelope")
        if not isinstance(self.definition["step"], dict) or not isinstance(
            self.definition["input_schemas"], dict
        ):
            raise ValueError("invalid method definition body")
        return self


class CapabilityMethodCatalog(_Catalog[Literal["knowbench/dynamic-methods/v3"]]):
    """Author-bound capabilities and optional inputs; legacy schemas stay strict."""

    planning: CapabilityPlanning
    public_capabilities: tuple[PublicCapability, ...] = Field(
        min_length=1, max_length=30
    )
    method_units: tuple[SlottedMethodUnit, ...] = Field(min_length=1, max_length=30)
    method_compiler: Literal["knowlab/workdraft-compiler/v2"]
    method_sources: dict[Identifier, SlottedMethodSource] = Field(max_length=30)

    @model_validator(mode="after")
    def _capabilities_and_sources(self) -> Self:
        units = {unit.contract.step_id: unit for unit in self.method_units}
        caps = {cap.id for cap in self.public_capabilities}
        if len(caps) != len(self.public_capabilities):
            raise ValueError("public capability ids must be unique")
        if caps != self.planning.capability_bindings.keys():
            raise ValueError("every public capability needs its exact private binding")
        for binding in self.planning.capability_bindings.values():
            if not set(binding.required_units) <= units.keys():
                raise ValueError("capability names an unknown method")
            for out in binding.evidence_outputs:
                if out.field not in units[out.method].contract.output_schema:
                    raise ValueError("capability names an unknown output field")
            needed = (
                set(self.planning.final_units)
                | set(binding.required_units)
                | {alias for alias, unit in units.items() if unit.selection.required}
            )
            pending = list(needed)
            while pending:
                for upstream in input_methods(
                    units[pending.pop()].contract, required_only=True
                ):
                    if upstream not in needed:
                        needed.add(upstream)
                        pending.append(upstream)
            if len(needed) > self.planning.max_steps:
                raise ValueError("step limit cannot deliver the declared capability")
        if not self.method_sources.keys() <= units.keys():
            raise ValueError("method source must name a catalog alias")
        for alias, source in self.method_sources.items():
            if source.definition.get("schema") == "knowlab/method-definition/v3":
                _validate_atomic_binding(units[alias], source, units)
                continue
            original = source.definition["step"]
            schemas = source.definition["input_schemas"]
            slots = original.get("uses")
            selection = original.get("selection")
            unit = units[alias]
            ref = source.reference
            if (
                original.get("id") != "method"
                or not isinstance(slots, list)
                or any(not isinstance(slot, str) for slot in slots)
                or not isinstance(selection, dict)
                or len(set(slots)) != len(slots)
                or set(slots) != set(schemas)
                or set(slots) != set(ref.inputs)
                or tuple(slots) != unit.contract.input_refs
            ):
                raise ValueError("method slots differ from the immutable definition")
            required = source.definition.get(
                "input_required", dict.fromkeys(slots, True)
            )
            if (
                not isinstance(required, dict)
                or set(required) != set(slots)
                or any(type(value) is not bool for value in required.values())
            ):
                raise ValueError("method input requirements are invalid")
            if (
                selection.get("required") is not False
                or type(selection.get("max_calls")) is not int
                or unit.selection.max_calls > selection["max_calls"]
                or unit.selection.applies_when != selection.get("applies_when")
                or unit.contract.step_version != str(ref.version)
            ):
                raise ValueError("expert selection differs from the frozen method")
            for slot, upstream in ref.inputs.items():
                binding = unit.contract.input_slots[slot]
                if binding.method_id != upstream or binding.required != required[slot]:
                    raise ValueError(
                        "expert cannot change the method input requirements"
                    )
                if upstream not in units or not isinstance(schemas[slot], dict):
                    raise ValueError("method input has no compatible upstream")
                provided = units[upstream].contract.output_schema
                if any(
                    key not in provided or provided[key] != value
                    for key, value in schemas[slot].items()
                ):
                    raise ValueError("method input schema differs from upstream output")
        return self


def _validate_atomic_binding(
    unit: SlottedMethodUnit,
    source: SlottedMethodSource,
    units: dict[str, SlottedMethodUnit],
) -> None:
    """Verify the provider's method/slot meaning and the expert's execution policy.

    The complete frozen pack still owns execution; this is not another author
    compiler or a route for the runtime planner to modify a method definition.
    """
    method = source.definition
    ref = source.reference
    inputs = method["inputs"]
    if (
        set(inputs) != set(ref.inputs)
        or tuple(sorted(inputs)) != unit.contract.input_refs
    ):
        raise ValueError("atomic method slots differ from the pinned definition")
    if (
        unit.contract.step_version != str(ref.version)
        or unit.selection.applies_when != method["applies_when"]
        or unit.contract.output_schema != method["output_schema"]
    ):
        raise ValueError("atomic method semantics differ from the compiled contract")
    for slot, expected in inputs.items():
        if not isinstance(expected, dict) or set(expected) != {
            "schema",
            "required",
            "description",
        }:
            raise ValueError("invalid atomic method input")
        upstream = ref.inputs[slot]
        binding = unit.contract.input_slots[slot]
        if (
            upstream not in units
            or binding.method_id != upstream
            or type(expected["required"]) is not bool
            or binding.required != expected["required"]
            or not isinstance(expected["schema"], dict)
        ):
            raise ValueError("atomic input binding differs from the author input")
        provided = units[upstream].contract.output_schema
        if any(
            key not in provided or provided[key] != value
            for key, value in expected["schema"].items()
        ):
            raise ValueError("upstream output is incompatible with the atomic input")
    policy = ref.execution
    assert policy is not None
    if set(policy) != {"ask_before", "on_fail", "advanced"}:
        raise ValueError("invalid expert execution policy")
    failure = policy["on_fail"]
    if not isinstance(failure, dict) or failure.get("kind") not in {
        "ask",
        "back",
        "rework",
    }:
        raise ValueError("invalid expert failure handling")
    if (
        unit.contract.on_reject
        != {"ask": "human", "back": "back", "rework": "rework"}[failure["kind"]]
    ):
        raise ValueError("failure handling differs from the expert policy")
    target = failure.get("back_to")
    inverse = {alias: slot for slot, alias in ref.inputs.items()}
    if failure["kind"] == "back" and (
        target not in inverse or unit.contract.back_to != inverse[target]
    ):
        raise ValueError("back target differs from the expert composition")
    if not isinstance(policy["ask_before"], list) or any(
        not isinstance(question, dict)
        or set(question) != {"id", "question", "options"}
        or not isinstance(question["options"], list)
        for question in policy["ask_before"]
    ):
        raise ValueError("invalid expert questions")
    expected_questions = [
        {
            "id": question["id"],
            "question": question["question"],
            "allow_other": True,
            "options": [
                {"id": f"o{i}", "label": label}
                for i, label in enumerate(question["options"], 1)
            ],
        }
        for question in policy["ask_before"]
    ]
    if [
        question.model_dump(mode="json") for question in unit.contract.ask_before
    ] != expected_questions:
        raise ValueError("questions differ from the expert policy")
    advanced = policy["advanced"]
    if not isinstance(advanced, dict) or advanced.get("output_schema") is not None:
        raise ValueError("execution policy cannot override method output semantics")
    for name in ("tools", "reserve", "input_max_chars", "output_file"):
        compiled = getattr(unit.contract, name)
        if name == "tools":
            compiled = list(compiled)
        if compiled != advanced.get(name):
            raise ValueError("compiled execution differs from the expert policy")


MethodCatalog = DynamicMethodCatalog | ReferencedMethodCatalog | CapabilityMethodCatalog
_CATALOG = TypeAdapter(MethodCatalog)


def parse_catalog(document: object) -> MethodCatalog:
    return _CATALOG.validate_python(document)


def render(*, referenced: bool = False, capabilities: bool = False) -> str:
    version = "v3" if capabilities else "v2" if referenced else "v1"
    model = (
        CapabilityMethodCatalog
        if capabilities
        else ReferencedMethodCatalog
        if referenced
        else DynamicMethodCatalog
    )
    return (
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"https://contracts.sunmoonai.com/knowbench/dynamic-methods/{version}/catalog.schema.json",
                **model.model_json_schema(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    print(render(), end="")
