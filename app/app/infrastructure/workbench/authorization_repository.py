"""Method authorization is runtime authority, not a second execution ledger."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.errors import StaleStateVersion


class AuthorizationRepositoryMixin:
    session: AsyncSession

    async def method_authorization(self, owner, call_id, *, lock=False):
        result = await self.session.execute(
            text(
                """SELECT document FROM workbench_method_authorizations
                WHERE owner_actor_id=:owner AND call_id=:id"""
                + (" FOR UPDATE" if lock else "")
            ),
            {"owner": owner, "id": call_id},
        )
        return result.scalar_one_or_none()

    async def save_method_authorization(self, owner, session_id, document, *, previous):
        params = {
            "owner": owner,
            "session": session_id,
            "id": document["call_id"],
            "revision": document["revision"],
            "document": json.dumps(document),
            "previous": previous,
        }
        if previous == 0:
            statement = """INSERT INTO workbench_method_authorizations
                (call_id,owner_actor_id,session_id,revision,document)
                SELECT c.id,:owner,:session,:revision,cast(:document as jsonb)
                FROM workbench_calls c
                WHERE c.id=:id AND c.owner_actor_id=:owner AND c.session_id=:session
                  AND c.kind='method'
                ON CONFLICT(call_id) DO NOTHING RETURNING call_id"""
        else:
            statement = """UPDATE workbench_method_authorizations
                SET revision=:revision,document=cast(:document as jsonb),updated_at=now()
                WHERE call_id=:id AND owner_actor_id=:owner AND session_id=:session
                  AND revision=:previous RETURNING call_id"""
        result = await self.session.execute(text(statement), params)
        if result.scalar_one_or_none() is None:
            raise StaleStateVersion("method authorization changed")
