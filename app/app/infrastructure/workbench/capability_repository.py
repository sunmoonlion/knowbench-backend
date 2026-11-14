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
                    "detail - 'resources' AS detail,secret,request_id,request_digest,created_at,updated_at "
                    "FROM workbench_capability_assets WHERE owner_actor_id=:o ORDER BY id"
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

    async def capability_insert(self, asset):
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
          SET revision=revision+1,status=:status,default_selected=CASE WHEN :status='disconnected' THEN false ELSE default_selected END,detail=cast(:detail as jsonb),secret=:secret,updated_at=now()
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
        await self.session.execute(
            text(
                """UPDATE workbench_capability_selections
            SET runtime=runtime || cast(:patch as jsonb)
            WHERE session_id=:s AND """
                + condition
            ),
            {"s": session_id, "scope": scope, "t": turn_id, "patch": json.dumps(patch)},
        )

    async def capability_revoke_sandbox(self, sandbox_id):
        # A new runner/transport cannot prove continuity of an old turn. Close
        # its authority before accepting more requests, including Skill reads.
        await self.session.execute(
            text("""UPDATE workbench_capability_selections c
                SET runtime=runtime || cast(:patch as jsonb)
                FROM workbench_sessions s
                WHERE c.session_id=s.id AND s.sandbox_id=:sandbox
                AND c.runtime->>'status' IN ('prepared','applied','completed')"""),
            {
                "sandbox": sandbox_id,
                "patch": json.dumps({"status": "completed", "aborted": True}),
            },
        )

    async def capability_calls(self, owner, session_id):
        await self.get_session(session_id, owner_actor_id=owner)
        return [
            row(r)
            for r in (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_calls WHERE origin='managed' AND owner_actor_id=:o AND session_id=:s ORDER BY created_at DESC LIMIT 100"
                    ),
                    {"o": owner, "s": session_id},
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
