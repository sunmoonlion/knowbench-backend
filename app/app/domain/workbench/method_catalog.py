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
from typing import Any, Generic, Literal, Self, TypeVar
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from app.domain.workbench.packs import Scope, StepContract, Strict

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
            for ref in units[key].contract.input_refs:
                visit(ref)
            visiting.remove(key)
            visited.add(key)

        required = set(self.planning.final_units) | {
            key for key, unit in units.items() if unit.selection.required
        }
        for key in required:
            visit(key)
        if len(visited) > self.planning.max_steps:
            raise ValueError("step limit cannot cover required methods and inputs")
        for key in units:
            visit(key)
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


MethodCatalog = DynamicMethodCatalog | ReferencedMethodCatalog
_CATALOG = TypeAdapter(MethodCatalog)


def parse_catalog(document: object) -> MethodCatalog:
    return _CATALOG.validate_python(document)


def render(*, referenced: bool = False) -> str:
    version = "v2" if referenced else "v1"
    model = ReferencedMethodCatalog if referenced else DynamicMethodCatalog
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
