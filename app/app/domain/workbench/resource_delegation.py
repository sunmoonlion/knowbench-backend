"""Reviewed resource-owner delegation evidence, separate from source licenses.

Records are operator-verified inputs, not grants created by a browser or model.
They permit an explicitly confirmed, single parent-to-child authorization; they
never grant an operation, prove method incubation, or replace action approval.
"""

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.method_capabilities import AccessRequirement

Digest = Annotated[str, Field(strict=True, pattern=r"^sha256:[a-f0-9]{64}$")]
RecordId = Annotated[
    str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
]
Identity = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceAuthorityRef(Record):
    record_id: RecordId
    revision: int = Field(strict=True, ge=1)
    digest: Digest


class DelegationBinding(Record):
    # This is the existing accepted-plan identity, not a formal MethodRef.
    # A future formal-method dispatcher needs its own explicit binding variant.
    kind: Literal["accepted_plan"] = "accepted_plan"
    parent_method_id: Identity
    parent_snapshot_digest: Digest
    child_method_id: Identity
    child_method_version: Identity


class DelegationEvidence(Record):
    reference: str = Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
    digest: Digest


def covers_requirement(ceiling: AccessRequirement, need: AccessRequirement) -> bool:
    """One record must cover the whole need; never union accounts or purposes."""
    if (
        ceiling.capability_ref != need.capability_ref
        or ceiling.version != need.version
        or not set(need.operations) <= set(ceiling.operations)
        or ceiling.data_transfer.destination != need.data_transfer.destination
        or ceiling.data_transfer.purpose != need.data_transfer.purpose
        or not set(need.data_transfer.argument_fields)
        <= set(ceiling.data_transfer.argument_fields)
    ):
        return False
    allowed = {
        (scope.resource_kind, scope.resource_role): set(scope.values)
        for scope in ceiling.resources
    }
    requested = {
        (scope.resource_kind, scope.resource_role): set(scope.values)
        for scope in need.resources
    }
    return allowed.keys() == requested.keys() and all(
        values <= allowed[role] for role, values in requested.items()
    )


class ResourceDelegation(Record):
    record_id: RecordId
    revision: int = Field(strict=True, ge=1)
    issuer: str = Field(strict=True, min_length=1, max_length=200, pattern=r"\S")
    owner_actor_id: UUID
    binding: DelegationBinding
    connection_id: UUID
    connection_revision: int = Field(strict=True, ge=1)
    operation_digest: str = Field(strict=True, pattern=r"^[a-f0-9]{64}$")
    requirement: AccessRequirement
    max_delegation_depth: int = Field(default=1, strict=True, ge=1, le=1)
    status: Literal["active", "revoked"]
    valid_from: datetime
    expires_at: datetime
    evidence: DelegationEvidence

    @model_validator(mode="after")
    def valid_period(self):
        if self.valid_from.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Resource authority times must include a timezone")
        if self.expires_at <= self.valid_from:
            raise ValueError("Resource authority expiry must follow its start")
        return self

    @property
    def reference(self) -> ResourceAuthorityRef:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return ResourceAuthorityRef(
            record_id=self.record_id,
            revision=self.revision,
            digest="sha256:" + hashlib.sha256(encoded).hexdigest(),
        )


class DelegationOption(Record):
    tool_key: str = Field(min_length=1, max_length=256)
    authority: ResourceAuthorityRef
    issuer: str = Field(min_length=1, max_length=200)
    expires_at: datetime
    binding: DelegationBinding


class ResourceDelegationCatalog:
    def __init__(self, records: tuple[ResourceDelegation, ...] = ()):
        self._records = {record.record_id: record for record in records}
        if len(self._records) != len(records):
            raise ValueError("Duplicate resource delegation record identity")

    def options(
        self,
        owner,
        *,
        binding: DelegationBinding,
        connection_id,
        connection_revision: int,
        operation_digest: str,
        requirement: AccessRequirement,
        now: datetime,
    ) -> list[ResourceDelegation]:
        if now.tzinfo is None:
            raise ValueError("Resource authority check requires an aware time")
        return [
            record
            for record in self._records.values()
            if str(record.owner_actor_id) == str(owner)
            and record.binding == binding
            and str(record.connection_id) == str(connection_id)
            and record.connection_revision == connection_revision
            and record.operation_digest == operation_digest
            and record.status == "active"
            and record.valid_from <= now < record.expires_at
            and covers_requirement(record.requirement, requirement)
        ]

    def require(
        self, reference: ResourceAuthorityRef, owner, **context
    ) -> ResourceDelegation:
        reference = ResourceAuthorityRef.model_validate(reference)
        for record in self.options(owner, **context):
            if record.reference == reference:
                return record
        raise ValueError(
            "Resource delegation authority changed or does not cover this invocation"
        )
