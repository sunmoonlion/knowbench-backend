"""Immutable tool discovery records. Registration is never an authorization grant."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError
from referencing.exceptions import Unresolvable

from app.domain.workbench.capabilities import CapabilityError, digest, tool_key
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.tool_schema import schema_validator

ToolSource = Literal[
    "platform", "knowbase", "mcp", "a2a", "skill_script", "skill_instruction"
]


@dataclass(frozen=True)
class ToolRegistration:
    source: ToolSource
    service: str
    name: str
    descriptor_json: str

    @classmethod
    def discover(
        cls, source: ToolSource, service: str, descriptor: dict[str, Any]
    ) -> ToolRegistration:
        """Copy supported discovery fields; remote policy claims are not imported."""
        name = descriptor.get("name")
        description = descriptor.get("description", "")
        schema = descriptor.get("inputSchema")
        if (
            not service
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 128
            or not isinstance(description, str)
            or not isinstance(schema, dict)
        ):
            raise CapabilityError("Invalid tool registration")
        schema_validator(schema)
        # JSON storage makes nested schemas immutable too; a caller receives a copy.
        material = {"name": name, "description": description, "inputSchema": schema}
        if source in ("platform", "knowbase"):
            # Preserve provider-owned presentation hints. They are not policy input.
            for field in ("annotations", "title", "outputSchema"):
                if field in descriptor:
                    material[field] = descriptor[field]
            if "outputSchema" in descriptor:
                schema_validator(descriptor["outputSchema"])
        try:
            encoded = json.dumps(
                material, sort_keys=True, ensure_ascii=False, allow_nan=False
            )
        except (ValueError, TypeError, RecursionError) as exc:
            raise CapabilityError("Tool descriptor must be finite JSON") from exc
        return cls(source, service, name, encoded)

    @property
    def tool_id(self) -> str:
        """Definition identity excludes user accounts and connection revisions."""
        return digest([self.source, self.service, self.name])

    @property
    def descriptor_digest(self) -> str:
        """Discovery fingerprint, not a promise that remote executable code is fixed."""
        return digest(self.descriptor())

    def descriptor(self) -> dict[str, Any]:
        return json.loads(self.descriptor_json)

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        validator = schema_validator(self.descriptor()["inputSchema"])
        try:
            valid = validator.is_valid(arguments)
        except (Unresolvable, RecursionError, ValueError) as exc:
            raise CapabilityError(
                "Tool input schema cannot be resolved locally"
            ) from exc
        if not valid:
            raise CapabilityError("Arguments do not match the registered schema")


class ToolRegistry:
    """A view over authoritative definitions, not a second database or global cache."""

    def __init__(self, registrations: Iterable[ToolRegistration]):
        self._tools: dict[str, ToolRegistration] = {}
        for tool in registrations:
            if tool.tool_id in self._tools:
                raise CapabilityError("Duplicate tool registration")
            self._tools[tool.tool_id] = tool

    @classmethod
    def snapshot(cls, source: ToolSource, service: str, descriptors: Iterable[dict]):
        return cls(
            ToolRegistration.discover(source, service, tool) for tool in descriptors
        )

    @classmethod
    def resolve(
        cls, source: ToolSource, service: str, descriptors: Iterable[dict], name: str
    ):
        """Look up one entry in the selected binding, rejecting ambiguous old snapshots."""
        selected = (tool for tool in descriptors if tool.get("name") == name)
        catalog = cls.snapshot(source, service, selected)
        return catalog.get(digest([source, service, name]))

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return tuple(self._tools.values())

    def get(self, tool_id: str) -> ToolRegistration:
        try:
            return self._tools[tool_id]
        except KeyError as exc:
            raise CapabilityError("Tool is not registered") from exc

    def descriptors(self) -> list[dict[str, Any]]:
        """Expose a single MCP namespace only when all names are unambiguous."""
        names = [tool.name for tool in self._tools.values()]
        if len(names) != len(set(names)):
            raise CapabilityError("Tool names collide in this MCP namespace")
        return [tool.descriptor() for tool in self._tools.values()]


def connection_source(asset: dict) -> Literal["mcp", "a2a"]:
    protocol = (asset["detail"].get("oauth") or {}).get("protocol", "mcp")
    if protocol not in ("mcp", "a2a"):
        raise CapabilityError("Unsupported registered connection protocol")
    return protocol


def connection_registration(asset: dict, name: str) -> ToolRegistration:
    if asset["kind"] != "connection":
        raise CapabilityError("Tool requires a connection")
    detail = asset["detail"]
    return ToolRegistry.resolve(
        connection_source(asset), detail["url"], detail.get("tools", []), name
    )


def formal_tool_key(reference: MethodRef) -> str:
    """Stable formal entry namespace. The backing installation can serve many methods."""
    return "formal_method_" + digest(reference.model_dump(mode="json"))[:32]


def check_session_entry(key: str, spec: dict, *, probe: bool) -> None:
    """Only server-prepared entry references select a route. No fallback by tool name."""
    from app.domain.workbench.formal_model import PROBE_TOOL

    kind = spec.get("execution_kind")
    if probe:
        if kind == "formal_probe" and key == PROBE_TOOL == spec.get("tool", {}).get(
            "name"
        ):
            return
    elif kind == "formal_method_model":
        try:
            reference = MethodRef.model_validate(
                (spec.get("formal_method") or {}).get("reference")
            )
        except ValidationError as exc:
            raise CapabilityError("Missing frozen formal method reference") from exc
        if key == formal_tool_key(reference) == spec.get("tool", {}).get("name"):
            return
    elif (
        kind in (None, "skill_script")
        and "formal_method" not in spec
        and "implementation_tool" not in spec
    ):
        asset = spec.get("source_asset_id")
        name = spec.get("tool", {}).get("name")
        if (
            isinstance(asset, str)
            and isinstance(name, str)
            and key == tool_key(asset, name)
        ):
            return
    raise CapabilityError("Tool entry does not match its registered invocation path")
