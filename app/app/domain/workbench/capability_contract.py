"""Provider-owned v1 response contracts for managed capabilities."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.domain.workbench.capabilities import Strict
from app.domain.workbench.formal_method_admission_generated import FormalMethodSource
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.method_capabilities import (
    AccessRequirement,
    McpRequirement,
    MethodCapabilities,
)
from app.domain.workbench.registry_sources import RegisteredSource
from app.domain.workbench.resource_delegation import (
    DelegationOption,
    ResourceAuthorityRef,
)
from app.domain.workbench.skill_dependencies import SkillDependency
from app.domain.workbench.skill_installation import SkillSource


class Tool(Strict):
    name: str
    description: str
    inputSchema: dict[str, Any]


class RegisteredTool(Tool):
    key: str


class OAuthAccount(Strict):
    provider_id: str
    protocol: Literal["mcp", "a2a"]
    issuer: str
    account_id: str
    scopes: list[str]


class ConnectionDetail(Strict):
    url: str
    account_label: str
    auth: Literal["none", "bearer", "oauth"]
    oauth: OAuthAccount | None = None
    reauth_reason: str | None = None
    tools: list[RegisteredTool]
    checked_at: str
    revocation: str | None = None


class SkillFile(Strict):
    path: str
    size: int
    sha256: str
    media_type: str


class SkillDetail(Strict):
    source: SkillSource | None = None
    description: str
    version: str
    digest: str
    dependencies: list[str]
    tool_requirements: list[McpRequirement] = Field(default_factory=list)
    dependency_status: list[SkillDependency] = Field(default_factory=list)
    format: Literal["instruction-markdown", "skill-package-zip"]
    files: list[SkillFile] = Field(default_factory=list)


class Asset(Strict):
    id: UUID
    kind: Literal["connection", "skill"]
    name: str
    revision: int
    status: Literal["active", "disabled", "disconnected", "reauth_required"]
    default_selected: bool
    detail: ConnectionDetail | SkillDetail
    created_at: datetime
    updated_at: datetime


class Service(Strict):
    url: str
    authentication: list[str]
    provider_id: str | None = None
    protocol: Literal["mcp", "a2a"] | None = None


class Catalog(Strict):
    script_execution_enabled: bool = False
    assets: list[Asset]
    services: list[Service]
    skill_formats: list[str]
    runtime_enabled: bool


class AvailableTool(Tool):
    key: str
    connection_id: UUID
    connection_name: str
    account_label: str
    status: str


class Issue(Strict):
    ref: str
    code: str


class MethodContext(Strict):
    task_id: UUID
    profile_id: str
    step_id: str | None
    declared_tools: list[str]
    managed_bindings_supported: bool
    context: str | None = None
    requirements: MethodCapabilities | None = None
    access_requirements: list[AccessRequirement] = Field(default_factory=list)
    permission_status: Literal[
        "not_required", "unconfirmed", "active", "revoked", "expired"
    ]
    delegation_options: list[DelegationOption] = Field(default_factory=list)


class Selection(Strict):
    session_id: UUID
    method: MethodContext | None
    revision: int
    tools: list[str]
    skills: list[str]
    formal_methods: tuple[MethodRef, ...] = Field(default=(), max_length=20)
    delegations: dict[str, ResourceAuthorityRef] = Field(default_factory=dict)
    skill_mode: Literal["auto", "explicit"]
    purpose: str
    editable: bool
    applied_revision: int | None
    preparation: str
    issues: list[Issue]
    available_tools: list[AvailableTool]
    available_skills: list[Asset]


class AttachmentPreview(Strict):
    name: str
    size: int
    sha256: str


class ApprovalResource(Strict):
    resource_kind: str
    resource_role: str
    values: list[str]


class ConnectorApproval(Strict):
    verified_zero_cost: bool
    operation: str
    effect: Literal["read", "write", "send"]
    destination: str
    arguments_digest: str
    resources: list[ApprovalResource]
    attachments: list[AttachmentPreview]
    read_until: datetime | None
    origin: UUID | None
    granted: bool
    revoked: bool


class CommonCallDetail(Strict):
    approval: ConnectorApproval | None = None
    connection_id: UUID | None
    revision: int
    connection_name: str
    account_label: str
    purpose: str
    tool: Tool
    arguments: dict[str, Any]
    approval_token: str = Field(repr=False)
    result: Any = None


class CallDetail(CommonCallDetail):
    source_asset_id: UUID | None = None
    execution_kind: Literal["skill_script"] | None = None
    selection_revision: int
    runtime: str
    method: dict[str, str] | None = None


class HostCallDetail(CommonCallDetail):
    execution_kind: Literal["formal_mcp", "formal_method"]
    source_asset_id: UUID
    connection_id: UUID


class FormalFunctionDetail(CommonCallDetail):
    material_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda v: v is None
    )
    execution_kind: Literal["formal_function"]
    source_asset_id: None
    connection_id: None
    registered_source: FormalMethodSource


class FormalScriptDetail(CommonCallDetail):
    execution_kind: Literal["formal_script"]
    source_asset_id: UUID
    connection_id: None
    script_quote: dict[str, Any]


class RegistryScriptDetail(CommonCallDetail):
    execution_kind: Literal["registry_script"]
    source_asset_id: None
    connection_id: None
    script_quote: dict[str, Any]
    source: RegisteredSource
    approval_digest: str


class ModelProbeCallDetail(CommonCallDetail):
    execution_kind: Literal["formal_probe"]
    source_asset_id: UUID
    connection_id: UUID
    selection_revision: int
    runtime: str


class RegisteredFunctionProbeDetail(CommonCallDetail):
    execution_kind: Literal["formal_probe"]
    source_asset_id: None
    connection_id: None
    registered_source: FormalMethodSource
    selection_revision: int
    runtime: str


class FormalModelCallDetail(CommonCallDetail):
    execution_kind: Literal["formal_method_model"]
    source_asset_id: UUID
    connection_id: UUID
    selection_revision: int
    runtime: str
    reference: MethodRef
    method_title: str


class Call(Strict):
    id: UUID
    owner_actor_id: UUID
    session_id: UUID
    source_asset_id: UUID | None = None
    connection_id: UUID | None
    interaction_id: UUID | None
    digest: str
    status: Literal[
        "awaiting_approval",
        "dispatching",
        "succeeded",
        "failed",
        "declined",
        "unknown",
        "expired",
    ]
    created_at: datetime
    updated_at: datetime
    detail: (
        CallDetail
        | HostCallDetail
        | RegistryScriptDetail
        | FormalFunctionDetail
        | FormalScriptDetail
        | ModelProbeCallDetail
        | RegisteredFunctionProbeDetail
        | FormalModelCallDetail
    )


class Calls(Strict):
    calls: list[Call]
