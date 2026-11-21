"""Operator-owned MCP operation mappings; discovery and model input cannot grant rights."""

from __future__ import annotations

import base64
import hashlib
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


class FormalExecutionCost(BaseModel):
    """Operator-reviewed absence of incremental provider fees for this adapter.

    Formal execution has no external billing ledger yet; an absent record closes
    that entry. Discovery, authors and remote annotations cannot supply it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    incremental_cost: Literal["0"]
    currency: Literal["CNY"]
    evidence_reference: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
    evidence_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


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
    formal_execution_cost: FormalExecutionCost | None = None
    # Opt-in profiles for reviewed adapters. Scoped reads return records tagged with their requested IDs.
    read_result_id_field: Name | None = None
    read_result_items_field: Name | None = None
    read_result_fields: tuple[Name, ...] = Field(default=(), max_length=64)
    inline_attachments_field: Name | None = None

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
        if self.read_result_id_field and (
            self.effect != "read"
            or len(self.resources) != 1
            or (self.resources[0].cardinality == "many")
            != bool(self.read_result_items_field)
        ):
            raise ValueError(
                "Reusable reads require one exact resource and a tagged result"
            )
        if self.read_result_items_field and not self.read_result_id_field:
            raise ValueError(
                "A result collection requires a reviewed resource identity"
            )
        if self.read_result_fields and (
            not self.read_result_id_field
            or len(set(self.read_result_fields)) != len(self.read_result_fields)
        ):
            raise ValueError(
                "Reviewed output fields require a scoped read and unique names"
            )
        if self.inline_attachments_field and self.effect not in ("write", "send"):
            raise ValueError(
                "Outbound attachments require a reviewed write or send operation"
            )
        roles = [r.resource_role for r in self.resources]
        paths = [r.argument_path for r in self.resources]
        if len(set(roles)) != len(roles) or len(set(paths)) != len(paths):
            raise ValueError("Duplicate operation resource role or argument path")
        return self

    @property
    def policy_digest(self) -> str:
        document = self.model_dump(mode="json")
        for field in (
            "formal_execution_cost",
            "read_result_id_field",
            "read_result_items_field",
            "inline_attachments_field",
        ):
            if document[field] is None:
                document.pop(field)
        if not self.read_result_fields:
            document.pop("read_result_fields")
        return digest(document)

    def resolve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        attachments = []
        if self.inline_attachments_field:
            files = arguments.get(self.inline_attachments_field, [])
            if not isinstance(files, list) or len(files) > 100:
                raise CapabilityError("Invalid outbound attachment list")
            for item in files:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"name", "content_base64"}
                    or not isinstance(item["name"], str)
                    or not 1 <= len(item["name"]) <= 255
                    or not isinstance(item["content_base64"], str)
                    or len(item["content_base64"]) > 64000
                ):
                    raise CapabilityError(
                        "Attachments require a file name and frozen inline bytes"
                    )
                try:
                    content = base64.b64decode(item["content_base64"], validate=True)
                except ValueError:
                    raise CapabilityError("Invalid attachment bytes") from None
                attachments.append(
                    {
                        "name": item["name"],
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
        return {
            **({"attachments": attachments} if self.inline_attachments_field else {}),
            **(
                {
                    "read_result_id_field": self.read_result_id_field,
                    "read_result_fields": list(self.read_result_fields),
                    "read_result_items_field": self.read_result_items_field,
                    "read_resource_path": list(self.resources[0].argument_path),
                }
                if self.read_result_id_field
                else {}
            ),
            **(
                {
                    "formal_execution_cost": self.formal_execution_cost.model_dump(
                        mode="json"
                    )
                }
                if self.formal_execution_cost
                else {}
            ),
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
    def __init__(
        self, operations: tuple[McpOperation, ...] = (), *, script_tariff=None
    ):
        self.script_tariff = script_tariff
        self._operations = {}
        for operation in operations:
            key = (operation.service_url, operation.tool_name)
            if key in self._operations:
                raise ValueError("Duplicate MCP operation binding")
            self._operations[key] = operation

    def formal_policy(self, registration):
        if registration.source in ("skill_script", "registered_script"):
            from app.domain.workbench.script_pricing import ScriptOperation

            if self.script_tariff is None:
                raise CapabilityError(
                    "No reviewed container tariff for this execution environment"
                )
            return ScriptOperation(self.script_tariff, registration)
        policy = self.require(registration)
        if policy.formal_execution_cost is None:
            raise CapabilityError(
                "The zero incremental cost policy must confirm that the provider charges no incremental fees"
            )
        return policy

    def require(self, tool: ToolRegistration) -> McpOperation:
        if tool.source not in ("mcp", "a2a"):
            raise CapabilityError(
                "Tool has no matching reviewed operation; contact the administrator"
            )
        return self.require_source(tool.service, tool.name, tool.descriptor_digest)

    def require_source(
        self, service_url: str, tool_name: str, descriptor_digest: str
    ) -> McpOperation:
        """Resolve an exact reviewed MCP descriptor without fabricating discovery."""
        policy = self._operations.get((service_url, tool_name))
        if policy is None or policy.descriptor_digest != descriptor_digest:
            raise CapabilityError(
                "Tool has no matching reviewed operation; contact the administrator"
            )
        return policy
