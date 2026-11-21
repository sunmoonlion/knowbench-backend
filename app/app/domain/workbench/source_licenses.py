"""Operator-recorded source rights, separate from discovery and user assertions.

The catalog checks the applicability of supplied review evidence. It neither
fetches that evidence nor independently establishes its legal validity. Empty
configuration grants nothing. Skill redistribution additionally requires reviewed terms.
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_method_admission_generated import SkillDeliveryTerms

Text = Annotated[str, Field(strict=True, min_length=1, max_length=20000, pattern=r"\S")]
Digest = Annotated[str, Field(strict=True, pattern=r"^sha256:[a-f0-9]{64}$")]


class LicenseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceIdentity(LicenseRecord):
    kind: Literal["mcp", "skill_instruction", "skill_script", "service_function"]
    source: Text
    version: Text
    digest: Digest
    entry: Text

    def key(self) -> tuple[str, ...]:
        return self.kind, self.source, self.version, self.digest, self.entry


class SourceRights(LicenseRecord):
    execute: bool = Field(strict=True)
    deliver: bool = Field(strict=True)


class LicenseEvidence(LicenseRecord):
    # An operator's stable audit/document reference; never fetched or exposed in
    # a source response. No query strings, credentials or free-form claims.
    reference: str = Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
    digest: Digest


class BusinessCoverageReview(LicenseRecord):
    """Independent review of the business scenarios covered by an exact test suite."""

    subject: Text
    method_id: UUID
    definition_digest: Digest
    source_digest: Digest
    suite_digest: Digest
    evidence: LicenseEvidence
    expires_at: AwareDatetime

    @property
    def fingerprint(self):
        return "sha256:" + digest(self.model_dump(mode="json"))


class SourceLicense(LicenseRecord):
    record_id: str = Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    revision: int = Field(strict=True, ge=1)
    subject: str = Field(
        strict=True, min_length=1, max_length=512, pattern=r"^[^\s*?]+$"
    )
    source: SourceIdentity
    rights: SourceRights
    status: Literal["active", "revoked"]
    valid_from: datetime
    expires_at: datetime
    evidence: LicenseEvidence
    delivery_terms: SkillDeliveryTerms | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def valid_period(self):
        if self.delivery_terms is not None and (
            not self.rights.deliver
            or self.source.kind not in ("skill_instruction", "skill_script")
        ):
            raise ValueError(
                "Skill delivery terms require a Skill redistribution right"
            )
        if self.valid_from.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Source license times must include a timezone")
        if self.expires_at <= self.valid_from:
            raise ValueError("Source license expiry must follow its start")
        return self

    @property
    def basis(self) -> str:
        # Hash every constraint, including evidence and delivery rights. Even an
        # accidental same-revision edit must invalidate a previously selected
        # source; old validation/frozen documents retain their original basis.
        fingerprint = digest(self.model_dump(mode="json"))
        return f"operator-source-license:{self.record_id}@{self.revision}:sha256:{fingerprint}"


class SourceLicenseCatalog:
    def __init__(self, records: tuple[SourceLicense, ...] = ()):
        self._records: dict[tuple[str, ...], SourceLicense] = {}
        ids: set[str] = set()
        for record in records:
            key = (record.subject, *record.source.key())
            if record.record_id in ids or key in self._records:
                raise ValueError("Duplicate or conflicting source license record")
            ids.add(record.record_id)
            self._records[key] = record

    def executable(
        self, subject: str, source: SourceIdentity, *, now: datetime
    ) -> SourceLicense | None:
        record = self._records.get((subject, *source.key()))
        if (
            record is None
            or record.status != "active"
            or not record.rights.execute
            or not record.valid_from <= now < record.expires_at
        ):
            return None
        return record

    def incubation_functions(self, subject: str, *, now: datetime):
        return [
            record
            for record in self._records.values()
            if record.subject == subject
            and record.source.kind == "service_function"
            and self.executable(subject, record.source, now=now) is not None
        ]

    def execution_basis(
        self, subject: str, source: SourceIdentity, *, now: datetime
    ) -> str:
        record = self.executable(subject, source, now=now)
        return record.basis if record else "pending"
