"""User-authored registry entries. Loading a revision does not dispatch it."""

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.workbench.formal_method_generated import Digest, Strict

SourceKind = Literal["script", "instruction"]


class SourceDraft(Strict):
    kind: SourceKind
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=262144)

    @field_validator("name", "content")
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > 262144:
            raise ValueError("Expected nonempty UTF-8 text up to 256 KiB")
        return value

    def content_digest(self):
        return "sha256:" + hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class RegisteredSource(Strict):
    source_id: UUID
    version: int
    kind: SourceKind
    name: str
    digest: Digest


class SourceRevision(RegisteredSource):
    content: str

    @classmethod
    def from_row(cls, row):
        from app.domain.workbench.capabilities import CapabilityError

        value = cls.model_validate({k: row[k] for k in cls.model_fields})
        if (
            SourceDraft(
                kind=value.kind, name=value.name, content=value.content
            ).content_digest()
            != value.digest
        ):
            raise CapabilityError(
                "Registered source content differs from its pinned digest"
            )
        return value
