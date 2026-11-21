"""Owner-scoped registration records. Neither view grants execution rights."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from app.domain.workbench.formal_method_generated import MethodRef, Strict


class InstallationEntry(Strict):
    kind: Literal["mcp", "a2a", "skill_instruction", "skill_script"]
    entry: str
    # Skill instructions load as package input and do not have a callable alias.
    tool_key: str | None = None


class InstalledRegistration(Strict):
    path: Literal["installed"] = "installed"
    asset_id: UUID
    revision: int
    status: Literal["active", "disabled", "disconnected", "reauth_required"]
    entries: list[InstallationEntry]


class FormalRegistration(Strict):
    path: Literal["formal"] = "formal"
    reference: MethodRef
    title: str
    # Direct methods pin an installation at incubation. Composite methods pin children.
    source_asset_id: UUID | None
    source_revision: int | None
    source_kind: Literal["platform", "mcp", "skill_instruction", "skill_script"] | None
    source_digest: str | None
    source_entry: str | None
    dependencies: tuple[MethodRef, ...]


class InstallationSummary(Strict):
    asset_id: UUID
    name: str
    kind: Literal["connection", "skill"]
    revision: int
    status: Literal["active", "disabled", "disconnected", "reauth_required"]
    account_label: str | None


class NativeRegistration(Strict):
    name: str
    item_type: str | None
    request: str | None
    interaction: Literal["approval", "question"] | None


class RegistryInventory(Strict):
    installed: list[InstallationSummary]
    formal: list[FormalRegistration]
    native: list[NativeRegistration]


class RegistryEvent(Strict):
    kind: Literal["call", "observation", "approval"]
    session_id: UUID
    record_id: str
    path: Literal["installed", "formal", "native", "platform", "runtime"]
    asset_id: UUID | None
    reference: MethodRef | None
    action: str
    status: str
    at: datetime
