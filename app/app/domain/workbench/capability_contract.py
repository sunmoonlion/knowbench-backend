"""Provider-owned v1 response contracts for managed capabilities."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.domain.workbench.capabilities import Strict


class Tool(Strict):
    name: str
    description: str
    inputSchema: dict[str, Any]


class RegisteredTool(Tool):
    key: str


class ConnectionDetail(Strict):
    url: str
    account_label: str
    auth: Literal["none", "bearer"]
    tools: list[RegisteredTool]
    checked_at: str
    revocation: str | None = None


class SkillFile(Strict):
    path: str
    size: int
    sha256: str
    media_type: str


class SkillDetail(Strict):
    description: str
    version: str
    content: str
    digest: str
    dependencies: list[str]
    format: Literal["instruction-markdown", "skill-package-zip"]
    files: list[SkillFile] = Field(default_factory=list)


class Asset(Strict):
    id: UUID
    kind: Literal["connection", "skill"]
    name: str
    revision: int
    status: Literal["active", "disabled", "disconnected"]
    default_selected: bool
    detail: ConnectionDetail | SkillDetail
    created_at: datetime
    updated_at: datetime


class Service(Strict):
    url: str
    authentication: list[str]


class Catalog(Strict):
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
    managed_bindings_supported: Literal[False]


class Selection(Strict):
    session_id: UUID
    method: MethodContext | None
    revision: int
    tools: list[str]
    skills: list[str]
    skill_mode: Literal["auto", "explicit"]
    purpose: str
    editable: bool
    applied_revision: int | None
    preparation: str
    issues: list[Issue]
    available_tools: list[AvailableTool]
    available_skills: list[Asset]


class CallDetail(Strict):
    connection_id: UUID
    revision: int
    connection_name: str
    account_label: str
    purpose: str
    tool: Tool
    arguments: dict[str, Any]
    selection_revision: int
    runtime: str
    approval_token: str = Field(repr=False)
    result: Any = None


class Call(Strict):
    id: UUID
    owner_actor_id: UUID
    session_id: UUID
    connection_id: UUID
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
    detail: CallDetail


class Calls(Strict):
    calls: list[Call]
