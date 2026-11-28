from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

NOT_BEFORE_HEADER = "sunmoonai.not_before.v1"


class OutboxEvent(BaseModel):
    """Transport-neutral event persisted with the owning business transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    aggregate_key: str = Field(min_length=1, max_length=512)
    deduplication_key: str = Field(min_length=1, max_length=512)
    payload: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)
    not_before: AwareDatetime | None = None

    @field_validator("not_before")
    @classmethod
    def normalize_not_before(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    def transport_headers(self) -> dict[str, str]:
        """Immutable scheduling intent; available_at is only its retry projection."""
        headers = dict(self.headers)
        if self.not_before is not None:
            headers[NOT_BEFORE_HEADER] = self.not_before.isoformat(
                timespec="microseconds"
            )
        return headers

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32 or any(
            not key
            or len(key) > 128
            or len(item) > 2048
            or "\n" in key
            or "\r" in key
            or "\n" in item
            or "\r" in item
            for key, item in value.items()
        ):
            raise ValueError("outbox headers exceed the transport contract")
        return value

    @model_validator(mode="after")
    def validate_serialized_size(self) -> OutboxEvent:
        if NOT_BEFORE_HEADER in self.headers:
            raise ValueError("use not_before, not the reserved delivery header")
        headers = self.transport_headers()
        self.validate_headers(headers)
        encoded = json.dumps(
            {"payload": self.payload, "headers": headers},
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(encoded) > 262_144:
            raise ValueError("outbox event exceeds 256 KiB")
        return self


class ClaimedOutboxEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    topic: str
    aggregate_key: str
    payload: dict[str, Any]
    headers: dict[str, str]
    attempt_count: int
