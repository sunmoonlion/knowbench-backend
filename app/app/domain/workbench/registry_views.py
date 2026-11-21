"""Owner-scoped registration records. Neither view grants execution rights."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import computed_field

from app.domain.workbench.formal_adapter import DIRECT_HOST_ADAPTERS
from app.domain.workbench.formal_method_admission_generated import Caller
from app.domain.workbench.formal_method_generated import MethodRef, Strict
from app.domain.workbench.registry_sources import RegisteredSource


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
    procedure_kind: Literal["direct", "orchestration"]
    allowed_callers: tuple[Caller, ...]

    @computed_field
    @property
    def method_kind(self) -> Literal["tool", "expert"]:
        return "expert" if self.procedure_kind == "orchestration" else "tool"

    @computed_field
    @property
    def runner_route(self) -> str:
        if self.procedure_kind == "orchestration":
            return "orchestration"
        return DIRECT_HOST_ADAPTERS.get(self.source_kind or "", "unavailable")

    # Direct methods pin an installation at incubation. Composite methods pin children.
    source_asset_id: UUID | None
    source_revision: int | None
    source_kind: (
        Literal[
            "service_function",
            "platform",
            "mcp",
            "skill_instruction",
            "skill_script",
            "registered_script",
            "registered_instruction",
        ]
        | None
    )
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
    kind: Literal["call", "approval", "observation", "task"]
    session_id: UUID
    record_id: str
    path: Literal[
        "installed", "formal", "native", "platform", "runtime", "registered_source"
    ]
    source: RegisteredSource | None = None
    asset_id: UUID | None
    reference: MethodRef | None
    action: str
    status: str
    at: datetime
