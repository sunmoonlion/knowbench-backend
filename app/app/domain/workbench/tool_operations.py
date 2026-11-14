"""Operator-owned MCP operation mappings; discovery and model input cannot grant rights."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.tool_registry import ToolRegistration

Name = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
ResourceValue = Annotated[str, Field(strict=True, min_length=1, max_length=2048)]


class ResourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_kind: Literal["connector"] = "connector"
    resource_role: Name
    # Object keys only: no expressions, wildcards, implicit defaults or array traversal.
    argument_path: tuple[Name, ...] = Field(min_length=1, max_length=8)
    cardinality: Literal["one", "many"] = "one"
    # None means no *operator* ceiling, never a user's authorization grant.
    allowed_values: tuple[ResourceValue, ...] | None = Field(
        default=None, min_length=1, max_length=1000
    )

    @model_validator(mode="after")
    def unique_values(self):
        if self.allowed_values is not None and len(set(self.allowed_values)) != len(
            self.allowed_values
        ):
            raise ValueError("Duplicate resource value")
        return self

    def resolve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        value: Any = arguments
        for key in self.argument_path:
            if not isinstance(value, dict) or key not in value:
                raise CapabilityError("Required operation resource is missing")
            value = value[key]
        values = [value] if self.cardinality == "one" else value
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 1000
            or any(not isinstance(v, str) or not v or len(v) > 2048 for v in values)
        ):
            raise CapabilityError("Operation resource has an unsupported value")
        resolved = sorted(set(values))
        if self.allowed_values is not None and not set(resolved).issubset(
            self.allowed_values
        ):
            raise CapabilityError("Operation resource is outside the platform scope")
        return {
            "resource_kind": self.resource_kind,
            "resource_role": self.resource_role,
            "values": resolved,
        }


class McpOperation(BaseModel):
    """A reviewed adapter definition, supplied only through operator settings.

    Exact resource sets are the first supported constraint. Provider-specific
    semantics (aliases, folder hierarchies, queries) need a reviewed adapter;
    this resolver must not guess them from names or remote annotations.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    service_url: str = Field(min_length=8, max_length=1000)
    tool_name: Name
    descriptor_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability_ref: Name
    version: Name
    operation: Name
    effect: Literal["read", "write", "send"]
    resources: tuple[ResourceBinding, ...] = Field(max_length=30)

    @model_validator(mode="after")
    def valid_definition(self):
        parts = urlsplit(self.service_url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Operation requires an HTTPS endpoint without credentials")
        roles = [r.resource_role for r in self.resources]
        paths = [r.argument_path for r in self.resources]
        if len(set(roles)) != len(roles) or len(set(paths)) != len(paths):
            raise ValueError("Duplicate operation resource role or argument path")
        return self

    @property
    def policy_digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def resolve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "policy_digest": self.policy_digest,
            "capability_ref": self.capability_ref,
            "version": self.version,
            "operation": self.operation,
            "effect": self.effect,
            "resources": [r.resolve(arguments) for r in self.resources],
            # Even a read can send sensitive query arguments to the provider.
            "data_transfer": {
                "destination": self.service_url,
                "arguments_digest": digest(arguments),
            },
        }


class OperationCatalog:
    def __init__(self, operations: tuple[McpOperation, ...] = ()):
        self._operations = {}
        for operation in operations:
            key = (operation.service_url, operation.tool_name)
            if key in self._operations:
                raise ValueError("Duplicate MCP operation binding")
            self._operations[key] = operation

    def require(self, tool: ToolRegistration) -> McpOperation:
        policy = self._operations.get((tool.service, tool.name))
        if (
            tool.source != "mcp"
            or policy is None
            or policy.descriptor_digest != tool.descriptor_digest
        ):
            raise CapabilityError(
                "Tool has no matching reviewed operation; contact the administrator"
            )
        return policy
