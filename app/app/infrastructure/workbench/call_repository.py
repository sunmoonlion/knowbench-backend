"""Shared persistent call identities. Business authorization stays in application code."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.errors import NotFound
from app.infrastructure.workbench.capability_repository import row


class CallRepositoryMixin:
    session: AsyncSession
    transaction: Callable[[], AbstractAsyncContextManager[Any]]

    async def call_by_key(self, owner, session_id, key):
        result = (
            (
                await self.session.execute(
                    text("""SELECT * FROM workbench_calls
          WHERE owner_actor_id=:o AND session_id=:s AND call_key=:k"""),
                    {"o": owner, "s": session_id, "k": key},
                )
            )
            .mappings()
            .first()
        )
        return row(result) if result else None

    async def add_call(self, call):
        columns = (
            "id",
            "owner_actor_id",
            "session_id",
            "kind",
            "origin",
            "call_key",
            "parent_call_id",
            "task_id",
            "attempt_id",
            "registration_id",
            "registration_digest",
            "source_asset_id",
            "connection_id",
            "interaction_id",
            "digest",
            "status",
        )
        values = {key: call.get(key) for key in columns}
        values["detail"] = json.dumps(call["detail"])
        result = (
            (
                await self.session.execute(
                    text(
                        f"INSERT INTO workbench_calls({','.join(columns)},detail) VALUES "
                        f"({','.join(':' + key for key in columns)},cast(:detail as jsonb)) "
                        "ON CONFLICT(owner_actor_id,session_id,call_key) DO NOTHING RETURNING *"
                    ),
                    values,
                )
            )
            .mappings()
            .first()
        )
        if result:
            return row(result)
        existing = await self.call_by_key(
            call["owner_actor_id"], call["session_id"], call["call_key"]
        )

        assert existing is not None
        return existing

    async def update_call(self, owner, call_id, status, detail):
        result = await self.session.execute(
            text("""UPDATE workbench_calls SET
          status=:status,detail=cast(:d as jsonb),updated_at=now()
          WHERE owner_actor_id=:o AND id=:id RETURNING id"""),
            {"o": owner, "id": call_id, "status": status, "d": json.dumps(detail)},
        )
        if result.scalar_one_or_none() is None:
            raise NotFound("call not found")

    async def update_method_call_state(self, task_id, state):
        # Projection of the authoritative Task; no independent method state machine.
        await self.session.execute(
            text("""UPDATE workbench_calls SET status=:state,
          updated_at=now() WHERE kind='method' AND parent_call_id IS NULL AND task_id=:id"""),
            {"id": task_id, "state": state.lower()},
        )

    async def recover_calls(self, owner, session_id):
        # Remote adapter has a 45 s deadline. After 2 min a lost dispatch receipt
        # is unknown, never retried. Expired approval cannot authorize an operation.
        async with self.transaction():
            await self.session.execute(
                text("""UPDATE workbench_calls c
                SET status=CASE WHEN c.status='dispatching' THEN 'unknown' ELSE 'expired' END,
                    updated_at=now()
                WHERE c.kind='tool' AND c.owner_actor_id=:o AND c.session_id=:s AND
                  ((c.status='dispatching' AND c.updated_at < now()-interval '2 minutes') OR
                   (c.status='awaiting_approval' AND EXISTS
                     (SELECT 1 FROM workbench_interactions i WHERE i.id=c.interaction_id AND i.expires_at <= now())))"""),
                {"o": owner, "s": session_id},
            )
