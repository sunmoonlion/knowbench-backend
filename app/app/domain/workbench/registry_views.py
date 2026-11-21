"""Owner-scoped registration records. Neither view grants execution rights."""

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
