"""Capability persistence, using the existing application transaction boundary."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.capabilities import default_selection
from app.domain.workbench.errors import IdempotencyConflict, NotFound, StaleStateVersion


def row(value):
    result = dict(value)
    for key in (
        "id",
        "owner_actor_id",
        "session_id",
        "connection_id",
        "interaction_id",
        "source_asset_id",
        "parent_call_id",
        "task_id",
        "attempt_id",
    ):
        if result.get(key) is not None:
            result[key] = str(result[key])
    return result


class CapabilityRepositoryMixin:
    session: AsyncSession
    transaction: Callable[[], AbstractAsyncContextManager[Any]]
    get_session: Callable[..., Awaitable[dict[str, Any]]]

    async def start_connection_oauth(
        self, owner, provider, state, verifier, context, expires
    ):
        await self.session.execute(
            text("DELETE FROM workbench_connection_oauth WHERE expires_at <= now()")
        )
        await self.session.execute(
            text("""INSERT INTO workbench_connection_oauth
            (state_hash,owner_actor_id,provider_id,verifier,context,expires_at)
            VALUES(:state,:owner,:provider,:verifier,cast(:context as jsonb),:expires)"""),
            {
                "state": state,
                "owner": owner,
                "provider": provider,
                "verifier": verifier,
                "context": json.dumps(context),
                "expires": expires,
            },
        )

    async def consume_connection_oauth(self, owner, provider, state):
        result = await self.session.execute(
            text("""DELETE FROM workbench_connection_oauth
            WHERE state_hash=:state AND owner_actor_id=:owner AND provider_id=:provider AND expires_at>now()
            RETURNING verifier, context"""),
            {"state": state, "owner": owner, "provider": provider},
        )
        return result.mappings().first()

    async def rotate_connection_secret(self, owner, asset_id, revision, secret):
        # Caller holds the current asset lock. Rotation preserves its authorization revision.
        await self.session.execute(
            text("""UPDATE workbench_capability_assets SET secret=:secret,updated_at=now()
            WHERE id=:id AND owner_actor_id=:owner AND revision=:revision AND status='active'"""),
            {"secret": secret, "id": asset_id, "owner": owner, "revision": revision},
        )

    async def capability_context_exists(self, owner, session_id):
        # Metadata-only fast path. Prior runtime state must still be reconciled,
        # even after all assets have been disabled or removed.
        return bool(
            (
                await self.session.execute(
                    text("""SELECT
            EXISTS (SELECT 1 FROM workbench_capability_assets WHERE owner_actor_id=:o)
            OR EXISTS (SELECT 1 FROM workbench_capability_selections
                WHERE session_id=:s AND runtime <> '{}'::jsonb)"""),
                    {"o": owner, "s": session_id},
                )
            ).scalar_one()
        )

    async def capability_assets(self, owner, *, lock=False):
        rows = (
            await self.session.execute(
                text(
                    "SELECT id,owner_actor_id,kind,name,revision,status,default_selected,"
                    "detail,created_at,updated_at FROM workbench_capability_assets "
                    "WHERE owner_actor_id=:o ORDER BY id"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"o": owner},
            )
        ).mappings()
        return [row(r) for r in rows]

    async def capability_asset(self, owner, asset_id, *, lock=False):
        q = (
            "SELECT * FROM workbench_capability_assets WHERE owner_actor_id=:o AND id=:id"
            + (" FOR UPDATE" if lock else "")
        )
        r = (
            (await self.session.execute(text(q), {"o": owner, "id": asset_id}))
            .mappings()
            .first()
        )
        if not r:
            raise NotFound("capability not found")
        return row(r)

    async def skill_file(self, owner, asset_id, path):
        data = (
            await self.session.execute(
                text("""
            SELECT f.data FROM workbench_skill_files f
            JOIN workbench_capability_assets a ON a.id=f.asset_id
            WHERE a.owner_actor_id=:owner AND a.kind='skill' AND a.status <> 'disconnected'
              AND f.asset_id=:id AND f.path=:path
        """),
                {"owner": owner, "id": asset_id, "path": path},
            )
        ).scalar_one_or_none()
        if data is None:
            raise NotFound("Skill file not found")
        return bytes(data)

    async def skill_package_files(self, owner, asset_id):
        rows = await self.session.execute(
            text("""
            SELECT f.path,f.data FROM workbench_skill_files f
            JOIN workbench_capability_assets a ON a.id=f.asset_id
            WHERE a.owner_actor_id=:owner AND a.kind='skill' AND f.asset_id=:id
        """),
            {"owner": owner, "id": asset_id},
        )
        return {path: bytes(data) for path, data in rows}

    async def capability_insert(self, asset, *, resources=None):
        async with self.transaction():
            result = (
                (
                    await self.session.execute(
                        text("""INSERT INTO workbench_capability_assets
            (id,owner_actor_id,kind,name,status,detail,secret,request_id,request_digest)
            VALUES (:id,:owner_actor_id,:kind,:name,:status,cast(:detail as jsonb),:secret,:request_id,:request_digest)
            ON CONFLICT (owner_actor_id,request_id) DO NOTHING RETURNING *"""),
                        {**asset, "detail": json.dumps(asset["detail"])},
                    )
                )
                .mappings()
                .first()
            )
            if result:
                if resources:
                    await self.session.execute(
                        text("""
                        INSERT INTO workbench_skill_files(asset_id,path,data)
                        VALUES (:id,:path,:data)
                    """),
                        [
                            {"id": asset["id"], "path": path, "data": data}
                            for path, data in resources.items()
                        ],
                    )
                return row(result)
            existing = (
                (
                    await self.session.execute(
                        text(
                            "SELECT * FROM workbench_capability_assets WHERE owner_actor_id=:o AND request_id=:r"
                        ),
                        {"o": asset["owner_actor_id"], "r": asset["request_id"]},
                    )
                )
                .mappings()
                .one()
            )
            if existing["request_digest"] != asset["request_digest"]:
                raise IdempotencyConflict("request id was used with other content")
            return row(existing)

    async def capability_update(
        self, owner, asset_id, revision, *, status, default_selected, detail, secret
    ):
        result = (
            (
                await self.session.execute(
                    text("""UPDATE workbench_capability_assets
          SET revision=revision+1,status=:status,default_selected=CASE WHEN :status IN ('disconnected','reauth_required') THEN false ELSE default_selected END,detail=cast(:detail as jsonb),secret=:secret,updated_at=now()
          WHERE id=:id AND owner_actor_id=:o AND revision=:revision RETURNING *"""),
                    dict(
                        id=asset_id,
                        o=owner,
                        revision=revision,
                        status=status,
                        default_selected=default_selected,
                        detail=json.dumps(detail),
                        secret=secret,
                    ),
                )
            )
            .mappings()
            .first()
        )
        if not result:
            raise StaleStateVersion("capability changed; reload before saving")
        return row(result)

    async def capability_default(self, owner, asset_id, revision, enabled):
        result = (
            (
                await self.session.execute(
                    text("""UPDATE workbench_capability_assets
            SET default_selected=:v, updated_at=now()
            WHERE owner_actor_id=:o AND id=:id AND revision=:r RETURNING *"""),
                    {"o": owner, "id": asset_id, "r": revision, "v": enabled},
                )
            )
            .mappings()
            .first()
        )
        if not result:
            raise StaleStateVersion("capability changed; reload before saving")
        return row(result)

    async def capability_initialize(self, owner, session_id):
        detail = default_selection(await self.capability_assets(owner))
        await self.session.execute(
            text("""INSERT INTO workbench_capability_selections
            (session_id,owner_actor_id,detail) VALUES (:s,:o,cast(:d as jsonb))"""),
            {"s": session_id, "o": owner, "d": json.dumps(detail)},
        )

    async def capability_selection(self, owner, session_id, *, lock=False):
        await self.get_session(session_id, owner_actor_id=owner)
        await self.session.execute(
            text("""INSERT INTO workbench_capability_selections(session_id,owner_actor_id)
          VALUES (:s,:o) ON CONFLICT DO NOTHING"""),
            {"s": session_id, "o": owner},
        )
        result = (
            (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_capability_selections WHERE session_id=:s AND owner_actor_id=:o"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"s": session_id, "o": owner},
                )
            )
            .mappings()
            .one()
        )
        return row(result)

    async def capability_save_selection(self, owner, session_id, revision, detail):
        result = (
            (
                await self.session.execute(
                    text("""UPDATE workbench_capability_selections SET revision=revision+1,detail=cast(:d as jsonb),updated_at=now()
          WHERE session_id=:s AND owner_actor_id=:o AND revision=:r RETURNING *"""),
                    {
                        "s": session_id,
                        "o": owner,
                        "r": revision,
                        "d": json.dumps(detail),
                    },
                )
            )
            .mappings()
            .first()
        )
        if not result:
            raise StaleStateVersion("selection changed; reload before saving")
        return row(result)

    async def capability_runtime(self, owner, session_id, runtime):
        await self.session.execute(
            text(
                "UPDATE workbench_capability_selections SET runtime=cast(:r as jsonb) WHERE session_id=:s AND owner_actor_id=:o"
            ),
            {"s": session_id, "o": owner, "r": json.dumps(runtime)},
        )

    async def capability_runtime_by_session(self, session_id):
        r = (
            (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_capability_selections WHERE session_id=:s"
                    ),
                    {"s": session_id},
                )
            )
            .mappings()
            .first()
        )
        return row(r) if r else None

    async def capability_runtime_event(
        self, session_id, scope, turn_id, *, started, aborted=False
    ):
        # Late events must never overwrite a newer preparation's token or scope.
        patch = (
            {"status": "applied", "turn_id": turn_id}
            if started
            else {"status": "completed", "aborted": aborted}
        )
        condition = (
            "runtime->>'status'='prepared' AND runtime->>'scope_id'=:scope"
            if started
            else "runtime->>'turn_id'=:t AND runtime->>'status'='applied'"
        )
        # An explicit stop can arrive before turn/start binds the host turn ID.
        # Preserve that decision even if the provider acknowledges its start later.
        binding_stop = (
            """ || CASE WHEN runtime ? 'pending_stop' AND EXISTS (
            SELECT 1 FROM workbench_commands
            WHERE session_id=:s AND kind='turn.interrupt' AND payload->>'turn_id'=:t
        ) THEN jsonb_build_object('status','completed','aborted',true) ELSE '{}'::jsonb END"""
            if started
            else ""
        )
        await self.session.execute(
            text(
                "UPDATE workbench_capability_selections SET runtime=runtime || cast(:patch as jsonb)"
                + binding_stop
                + " WHERE session_id=:s AND "
                + condition
            ),
            {"s": session_id, "scope": scope, "t": turn_id, "patch": json.dumps(patch)},
        )

    async def lock_capability_sandbox_scope(self, sandbox_id):
        # Match task cancellation and capability actions: Task -> Session ->
        # scope/interaction/call. Stable ordering also serializes bulk recovery.
        await self.session.execute(
            text("""SELECT t.id FROM workbench_tasks t
                JOIN workbench_sessions s ON s.id=t.session_id
                WHERE s.sandbox_id=:sandbox ORDER BY t.id FOR NO KEY UPDATE OF t"""),
            {"sandbox": sandbox_id},
        )
        await self.session.execute(
            text(
                "SELECT id FROM workbench_sessions WHERE sandbox_id=:sandbox ORDER BY id FOR NO KEY UPDATE"
            ),
            {"sandbox": sandbox_id},
        )

    async def capability_revoke_sandbox(self, sandbox_id):
        # A new runner/transport cannot prove continuity of an old turn. Close
        # its authority before accepting more requests, including Skill reads.
        await self.lock_capability_sandbox_scope(sandbox_id)
        await self.session.execute(
            text("""UPDATE workbench_capability_selections c
                SET runtime=(runtime - 'gateway_hash') || cast(:patch as jsonb)
                FROM workbench_sessions s
                WHERE c.session_id=s.id AND s.sandbox_id=:sandbox
                AND c.runtime->>'status' IN ('prepared','applied','completed')"""),
            {
                "sandbox": sandbox_id,
                "patch": json.dumps({"status": "completed", "aborted": True}),
            },
        )

    async def capability_calls(self, owner, session_id, call_id=None):
        await self.get_session(session_id, owner_actor_id=owner)
        return [
            row(r)
            for r in (
                await self.session.execute(
                    text(
                        """SELECT c.*, grant_call.read_revoked_at AS origin_read_revoked_at
                        FROM workbench_calls c LEFT JOIN workbench_calls grant_call
                        ON grant_call.id=cast(c.detail->>'read_approval_id' AS uuid)
                        AND grant_call.owner_actor_id=c.owner_actor_id AND grant_call.session_id=c.session_id
                        WHERE c.origin='managed' AND c.owner_actor_id=:o AND c.session_id=:s
                        AND (cast(:call_id AS uuid) IS NULL OR c.id=cast(:call_id AS uuid))
                        ORDER BY c.created_at DESC LIMIT 100"""
                    ),
                    {"o": owner, "s": session_id, "call_id": call_id},
                )
            ).mappings()
        ]

    async def capability_call_by_invocation(self, owner, session_id, invocation_key):
        result = (
            (
                await self.session.execute(
                    text("""SELECT * FROM workbench_calls
            WHERE owner_actor_id=:o AND session_id=:s AND call_key=:k AND origin='managed'"""),
                    {"o": owner, "s": session_id, "k": invocation_key},
                )
            )
            .mappings()
            .first()
        )
        return row(result) if result else None

    async def capability_pending_scripts(self):
        result = await self.session.execute(
            text("""SELECT * FROM workbench_calls
            WHERE status='dispatching' AND detail->>'execution_kind'='skill_script'
            ORDER BY updated_at LIMIT 20""")
        )
        return [row(r) for r in result.mappings()]

    async def capability_call(self, owner, call_id, *, lock=False):
        r = (
            (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_calls WHERE origin='managed' AND owner_actor_id=:o AND id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"o": owner, "id": call_id},
                )
            )
            .mappings()
            .first()
        )
        if not r:
            raise NotFound("call not found")
        return row(r)
