from __future__ import annotations

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin, UUIDMixin


class AuthUser(UUIDMixin, TimestampMixin, Base):
    """Local authorization binding keyed by the provider subject (issuer recorded)."""

    __tablename__ = "auth_user"

    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(256))
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        # 只按 subject 认人（账 175）：换了 Casdoor 地址同一个人还是同一份
        UniqueConstraint("subject", name="uq_auth_user_subject"),
        Index("ix_auth_user_subject", "subject"),
    )
