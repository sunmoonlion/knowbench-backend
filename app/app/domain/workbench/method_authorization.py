"""Typed, exact permission delegation; input values are deliberately absent.

This module compares already-authorized grants. Only the host may create them
from confirmation and resource-owner facts. A declaration alone is never one.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.method_capabilities import AccessRequirement, Requirement
from app.domain.workbench.resource_delegation import ResourceAuthorityRef

# Installed entries and author-registered versions occupy separate namespaces.
_SCRIPT_BINDINGS = {
    "skill_script": ("script_asset_id", "script_revision"),
    "skill_instruction": ("instruction_asset_id", "instruction_revision"),
    "registered_script": ("registered_script_id", "registered_script_version"),
}


def grant_binding(kind, identity, revision):
    id_field, revision_field = _SCRIPT_BINDINGS.get(
        kind, ("connection_id", "connection_revision")
    )
    return {id_field: identity, revision_field: revision}


class PermissionGrant(Requirement):
    grant_id: str = Field(min_length=1, max_length=128)
    confirmation_context: str = Field(pattern=r"^[a-f0-9]{64}$")
    connection_id: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    connection_revision: int | None = Field(
        default=None, strict=True, ge=1, exclude_if=lambda v: v is None
    )
    script_asset_id: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    script_revision: int | None = Field(
        default=None, strict=True, ge=1, exclude_if=lambda v: v is None
    )
    registered_script_id: UUID | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    registered_script_version: int | None = Field(
        default=None, strict=True, ge=1, exclude_if=lambda v: v is None
    )
    instruction_asset_id: UUID | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    instruction_revision: int | None = Field(
        default=None, strict=True, ge=1, exclude_if=lambda v: v is None
    )
    operation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirement: AccessRequirement
    delegable: bool = Field(strict=True)
    # A user confirmation names an exact host-created invocation. It is direct
    # authority for that recipient, never permission to delegate to descendants.
    # None is only useful with an explicitly established resource-owner grant.
    recipient_call_id: UUID | None
    expires_at: datetime
    # Resource-owner restrictions cannot be erased by the recipient. Empty means
    # no additional method restriction, not authority outside this grant.
    method_ids: tuple[str, ...] = Field(max_length=100)
    resource_authority: ResourceAuthorityRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def valid(self):
        revisions = (
            (self.connection_id, self.connection_revision),
            (self.script_asset_id, self.script_revision),
            (self.registered_script_id, self.registered_script_version),
            (self.instruction_asset_id, self.instruction_revision),
        )
        if (
            any((a is None) != (r is None) for a, r in revisions)
            or sum(a is not None for a, _ in revisions) != 1
        ):
            raise ValueError(
                "Specify exactly one connection, Skill or registered script revision"
            )
        if (
            self.script_asset_id
            or self.instruction_asset_id
            or self.registered_script_id
        ) and (
            self.resource_authority is not None
            or self.delegable
            or self.recipient_call_id is None
        ):
            raise ValueError(
                "A script or instruction permission must name a direct recipient"
            )
        if self.expires_at.tzinfo is None:
            raise ValueError("Permission expiry must include a timezone")
        if len(set(self.method_ids)) != len(self.method_ids):
            raise ValueError("Duplicate permission method restriction")
        if self.delegable and self.recipient_call_id is not None:
            raise ValueError("Direct invocation authority cannot also be delegable")
        if self.delegable and self.resource_authority is None:
            raise ValueError("Delegation requires a pinned resource-owner authority")
        if self.resource_authority is not None and not self.method_ids:
            raise ValueError("Resource delegation requires an exact method restriction")
        return self

    def matches_asset(self, kind, asset_id, revision):
        id_field, revision_field = _SCRIPT_BINDINGS.get(
            kind, ("connection_id", "connection_revision")
        )
        identity = getattr(self, id_field)
        return (
            identity is not None
            and str(identity) == str(asset_id)
            and getattr(self, revision_field) == revision
        )


class MethodAuthorization(Requirement):
    call_id: UUID
    revision: int = Field(strict=True, ge=1)
    parent_call_id: UUID | None
    parent_revision: Annotated[int, Field(strict=True, ge=1)] | None
    method_id: str = Field(min_length=1, max_length=200)
    revoked: bool = Field(strict=True)
    expires_at: datetime
    grants: tuple[PermissionGrant, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def valid(self):
        if self.expires_at.tzinfo is None:
            raise ValueError("Authorization expiry must include a timezone")
        if (self.parent_call_id is None) != (self.parent_revision is None):
            raise ValueError("Parent identity and revision must be recorded together")
        if self.parent_call_id == self.call_id:
            raise ValueError("An authorization cannot delegate to itself")
        if len({g.grant_id for g in self.grants}) != len(self.grants):
            raise ValueError("Duplicate permission grant")
        return self


def covers(
    parent: PermissionGrant,
    child: PermissionGrant,
    *,
    method_id: str,
    call_id: UUID,
) -> bool:
    """No union across grants, accounts, purposes or resource-role semantics."""
    a, b = parent.requirement, child.requirement
    if (
        parent.grant_id != child.grant_id
        or parent.confirmation_context != child.confirmation_context
        or parent.connection_id != child.connection_id
        or parent.connection_revision != child.connection_revision
        or parent.registered_script_id != child.registered_script_id
        or parent.registered_script_version != child.registered_script_version
        or parent.script_asset_id != child.script_asset_id
        or parent.script_revision != child.script_revision
        or parent.instruction_asset_id != child.instruction_asset_id
        or parent.instruction_revision != child.instruction_revision
        or parent.operation_digest != child.operation_digest
        or (parent.delegable and parent.resource_authority is None)
        or parent.resource_authority != child.resource_authority
        or (
            parent.resource_authority is not None
            and parent.delegable
            and (child.delegable or child.recipient_call_id != call_id)
        )
        or not (parent.delegable or parent.recipient_call_id == call_id)
        or (child.delegable and not parent.delegable)
        or (
            parent.recipient_call_id is not None
            and child.recipient_call_id != parent.recipient_call_id
        )
        or (child.recipient_call_id is not None and child.recipient_call_id != call_id)
        or child.expires_at > parent.expires_at
        or (parent.method_ids and method_id not in parent.method_ids)
        or (parent.method_ids and not set(child.method_ids) <= set(parent.method_ids))
        or (parent.method_ids and not child.method_ids)
        or a.capability_ref != b.capability_ref
        or a.version != b.version
        or not set(b.operations) <= set(a.operations)
        or a.data_transfer.destination != b.data_transfer.destination
        or a.data_transfer.purpose != b.data_transfer.purpose
        or not set(b.data_transfer.argument_fields)
        <= set(a.data_transfer.argument_fields)
    ):
        return False
    available = {(r.resource_kind, r.resource_role): set(r.values) for r in a.resources}
    requested = {(r.resource_kind, r.resource_role): set(r.values) for r in b.resources}
    return available.keys() == requested.keys() and all(
        values <= available[role] for role, values in requested.items()
    )


def check_delegation(
    parent: MethodAuthorization,
    child: MethodAuthorization,
    *,
    now: datetime | None = None,
) -> None:
    """Recheck live parent against the child's frozen ceiling, including on resume.

    New parent rights cannot enlarge the frozen child. A narrower parent may
    still cover a child; otherwise execution pauses, rather than dropping a
    required resource or falling back to the account owner's full permissions.
    """
    now = now or datetime.now(UTC)
    if (
        parent.revoked
        or child.revoked
        or parent.expires_at <= now
        or child.expires_at <= now
        or child.expires_at > parent.expires_at
        or child.parent_call_id != parent.call_id
        or child.parent_revision is None
        or child.parent_revision > parent.revision
    ):
        raise CapabilityError("Method parent authorization is no longer valid")
    available = {g.grant_id: g for g in parent.grants}
    for grant in child.grants:
        source = available.get(grant.grant_id)
        if (
            source is None
            or grant.expires_at <= now
            or source.expires_at <= now
            or not covers(
                source, grant, method_id=child.method_id, call_id=child.call_id
            )
        ):
            raise CapabilityError(
                "Required method permission exceeds its live parent scope"
            )


def delegate(
    parent: MethodAuthorization,
    *,
    call_id: UUID,
    method_id: str,
    requirements: tuple[PermissionGrant, ...],
) -> MethodAuthorization:
    """Host supplies the concrete child need/binding, separately from input values."""
    # Current resource evidence supports one hop only. A child receives direct
    # authority for its host-created invocation, never another delegable grant.
    narrowed = tuple(
        grant.model_copy(update={"delegable": False, "recipient_call_id": call_id})
        if grant.delegable and grant.resource_authority is not None
        else grant
        for grant in requirements
    )
    child = MethodAuthorization(
        call_id=call_id,
        revision=1,
        parent_call_id=parent.call_id,
        parent_revision=parent.revision,
        method_id=method_id,
        revoked=False,
        expires_at=parent.expires_at,
        grants=narrowed,
    )
    check_delegation(parent, child)
    return child
