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

    async def list_task_calls(self, owner, task_id):
        """Complete task call ledger for reconciliation, not a paginated UI view."""
        result = await self.session.execute(
            text("""SELECT * FROM workbench_calls
                WHERE owner_actor_id=:o AND task_id=:t ORDER BY created_at,id"""),
            {"o": owner, "t": task_id},
        )
        return [row(item) for item in result.mappings()]

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
          status=:status,detail=cast(:d as jsonb),updated_at=now(),receipt_retry_at=NULL,receipt_problem=NULL
          WHERE owner_actor_id=:o AND id=:id RETURNING id"""),
            {"o": owner, "id": call_id, "status": status, "d": json.dumps(detail)},
        )
        if result.scalar_one_or_none() is None:
            raise NotFound("call not found")

    async def grant_call_read_scope(self, owner, call_id, key, expires):
        await self.session.execute(
            text("""UPDATE workbench_calls
            SET read_scope_key=:key,read_scope_until=:expires
            WHERE owner_actor_id=:o AND id=:id AND status='awaiting_approval'"""),
            {"o": owner, "id": call_id, "key": key, "expires": expires},
        )

    async def active_call_read_scope(
        self, owner, session_id, key, resources, *, origin=None
    ):
        result = await self.session.execute(
            text("""SELECT id FROM workbench_calls
            WHERE owner_actor_id=:o AND session_id=:s AND read_scope_key=:key
            AND detail->'operation'->'resources' @> cast(:resources as jsonb)
            AND status='succeeded' AND read_scope_until>clock_timestamp() AND read_revoked_at IS NULL
            AND (cast(:origin AS uuid) IS NULL OR id=cast(:origin AS uuid))
            ORDER BY created_at DESC,id LIMIT 1"""),
            {
                "o": owner,
                "s": session_id,
                "key": key,
                "origin": origin,
                "resources": json.dumps(resources),
            },
        )
        value = result.scalar_one_or_none()
        return str(value) if value else None

    async def revoke_call_read_scope(self, owner, session_id, key, resources):
        await self.session.execute(
            text("""UPDATE workbench_calls SET read_revoked_at=now()
            WHERE owner_actor_id=:o AND session_id=:s AND read_scope_key=:key AND read_revoked_at IS NULL AND detail->'operation'->'resources' = cast(:resources as jsonb)"""),
            {
                "o": owner,
                "s": session_id,
                "key": key,
                "resources": json.dumps(resources),
            },
        )

    async def update_method_call_state(self, task_id, state):
        # Projection of the authoritative Task; no independent method state machine.
        await self.session.execute(
            text("""UPDATE workbench_calls SET status=:state,
          updated_at=now() WHERE kind='method' AND parent_call_id IS NULL AND task_id=:id"""),
            {"id": task_id, "state": state.lower()},
        )
        active = await self.session.scalar(
            text("SELECT active_attempt_id FROM workbench_tasks WHERE id=:id"),
            {"id": task_id},
        )
        if active is not None:
            await self.project_method_attempt(str(active))

    async def project_method_attempt(self, attempt_id):
        # A late receipt from a replaced dispatch cannot overwrite the current
        # projection. All transitions still originate from Task/Attempt.
        await self.session.execute(
            text("""UPDATE workbench_calls c SET
                status=CASE WHEN t.state='WAITING' AND a.status IN ('CREATED','RUNNING')
                            THEN 'waiting' ELSE lower(a.status) END,
                attempt_id=a.id,updated_at=now()
                FROM workbench_attempts a, workbench_tasks t
                WHERE a.id=:id AND t.id=a.task_id AND t.active_attempt_id=a.id
                  AND c.id::text=a.refs->>'method_call_id'
                  AND c.task_id=t.id AND c.kind='method'
                  AND c.parent_call_id IS NOT NULL"""),
            {"id": attempt_id},
        )

    async def recover_calls(self, owner, session_id):
        # Remote adapter has a 45 s deadline. After 2 min a lost dispatch receipt
        # is unknown, never retried. Expired/cancelled approvals cannot authorize
        # an operation; an undelivered intent remains distinct from unknown I/O.
        # Isolated Jobs retain their own receipts. Only their reconciler can
        # distinguish a finished Job from a lost submission after a restart.
        async with self.transaction():
            await self.session.execute(
                text("""UPDATE workbench_calls c
                SET status=CASE WHEN c.status='dispatching' THEN 'unknown' ELSE 'expired' END,
                    updated_at=now()
                WHERE c.kind='tool' AND c.owner_actor_id=:o AND c.session_id=:s AND
                  ((c.status='dispatching' AND c.updated_at < now()-interval '2 minutes'
                    AND (c.origin <> 'managed' OR coalesce(c.detail->>'execution_kind','') NOT IN ('skill_script','formal_script','registry_script'))
                    AND (c.detail->>'execution_kind' IS DISTINCT FROM 'formal_instruction' OR EXISTS
                      (SELECT 1 FROM workbench_tasks t WHERE t.id=c.task_id AND t.active_attempt_id IS DISTINCT FROM c.attempt_id))) OR
                   (c.status='awaiting_approval' AND EXISTS
                     (SELECT 1 FROM workbench_interactions i WHERE i.id=c.interaction_id
                       AND (i.expires_at <= now() OR i.status IN ('cancelled','expired')))))"""),
                {"o": owner, "s": session_id},
            )
