"""Exact grants returned by KnowMart for one authenticated incubation author."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.domain.workbench.formal_method_generated import MethodRef, Strict


class IncubationGrant(Strict):
    topic_hit: bool = False
    listing_id: UUID
    owner_subject: str = Field(min_length=1, max_length=512)
    reference: MethodRef
    scope: Literal["acquired", "platform"]


class IncubationGrants(Strict):
    subject: str
    items: tuple[IncubationGrant, ...] = Field(max_length=100)
    next: UUID | None
