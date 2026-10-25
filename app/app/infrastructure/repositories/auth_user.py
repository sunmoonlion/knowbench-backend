"""Local users in PostgreSQL."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from app.infrastructure.storage.postgres import get_postgres

UPSERT_USER = text(
    """
    INSERT INTO auth_user (
        id, issuer, subject, username, email, display_name, roles, scopes
    )
    VALUES (
        :id, :issuer, :subject, :username, :email, :display_name,
        CAST(:roles AS jsonb), CAST(:scopes AS jsonb)
    )
    ON CONFLICT (subject) DO UPDATE SET
        issuer = EXCLUDED.issuer,
        username = EXCLUDED.username,
        email = EXCLUDED.email,
        display_name = EXCLUDED.display_name,
        roles = EXCLUDED.roles,
        scopes = EXCLUDED.scopes,
        updated_at = NOW()
    RETURNING id, email, display_name, roles, scopes
    """
)


class SqlUserDirectory:
    """Looks the pool up on every call: it is built before PostgreSQL is initialized."""

    async def upsert(
        self,
        *,
        issuer: str,
        subject: str,
        username: str,
        email: str | None,
        display_name: str | None,
        roles: list[str],
        scopes: list[str],
    ) -> dict[str, Any]:
        async with get_postgres().session_factory() as session:
            result = await session.execute(
                UPSERT_USER,
                {
                    "id": uuid.uuid4(),
                    "issuer": issuer,
                    "subject": subject,
                    "username": username,
                    "email": email,
                    "display_name": display_name,
                    "roles": json.dumps(roles, separators=(",", ":")),
                    "scopes": json.dumps(scopes, separators=(",", ":")),
                },
            )
            row = result.mappings().one()
            await session.commit()
        return dict(row)
