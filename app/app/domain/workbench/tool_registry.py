"""Immutable tool discovery records. Registration is never an authorization grant."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from referencing.exceptions import Unresolvable

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.tool_schema import schema_validator

ToolSource = Literal["platform", "knowbase", "mcp", "skill_script"]


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
