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
    ):
        if result.get(key) is not None:
            result[key] = str(result[key])
    return result


class CapabilityRepositoryMixin:
    session: AsyncSession
    transaction: Callable[[], AbstractAsyncContextManager[Any]]
    get_session: Callable[..., Awaitable[dict[str, Any]]]

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
            "runtime->>'status'='prepared'" if started else "runtime->>'turn_id'=:t"
        )
        await self.session.execute(
            text(
                """UPDATE workbench_capability_selections
            SET runtime=runtime || cast(:patch as jsonb)
            WHERE session_id=:s AND runtime->>'scope_id'=:scope AND """
                + condition
            ),
            {"s": session_id, "scope": scope, "t": turn_id, "patch": json.dumps(patch)},
        )

    async def capability_calls(self, owner, session_id):
        await self.get_session(session_id, owner_actor_id=owner)
        return [
            row(r)
            for r in (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_capability_calls WHERE owner_actor_id=:o AND session_id=:s ORDER BY created_at DESC LIMIT 100"
                    ),
                    {"o": owner, "s": session_id},
                )
            ).mappings()
        ]

    async def capability_call_by_digest(self, owner, session_id, digest):
        result = (
            (
                await self.session.execute(
                    text("""SELECT * FROM workbench_capability_calls
            WHERE owner_actor_id=:o AND session_id=:s AND digest=:d"""),
                    {"o": owner, "s": session_id, "d": digest},
                )
            )
            .mappings()
            .first()
        )
        return row(result) if result else None

    async def capability_recover_calls(self, owner, session_id):
        # Remote adapter has a 45 s deadline. After 2 min a lost dispatch receipt
        # is unknown, never retried. Expired approval cannot authorize an operation.
        async with self.transaction():
            await self.session.execute(
                text("""UPDATE workbench_capability_calls c
                SET status=CASE WHEN c.status='dispatching' THEN 'unknown' ELSE 'expired' END,
                    updated_at=now()
                WHERE c.owner_actor_id=:o AND c.session_id=:s AND
                  ((c.status='dispatching' AND c.updated_at < now()-interval '2 minutes') OR
                   (c.status='awaiting_approval' AND EXISTS
                     (SELECT 1 FROM workbench_interactions i WHERE i.id=c.interaction_id AND i.expires_at <= now())))"""),
                {"o": owner, "s": session_id},
            )

    async def capability_call(self, owner, call_id, *, lock=False):
        r = (
            (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_capability_calls WHERE owner_actor_id=:o AND id=:id"
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

    async def capability_add_call(self, call):
        r = (
            (
                await self.session.execute(
                    text("""INSERT INTO workbench_capability_calls(id,owner_actor_id,session_id,connection_id,interaction_id,digest,status,detail)
          VALUES (:id,:owner_actor_id,:session_id,:connection_id,:interaction_id,:digest,:status,cast(:detail as jsonb))
          ON CONFLICT (session_id,digest) DO NOTHING RETURNING *"""),
                    {**call, "detail": json.dumps(call["detail"])},
                )
            )
            .mappings()
            .first()
        )
        if not r:
            r = (
                (
                    await self.session.execute(
                        text(
                            "SELECT * FROM workbench_capability_calls WHERE session_id=:s AND digest=:d"
                        ),
                        {"s": call["session_id"], "d": call["digest"]},
                    )
                )
                .mappings()
                .one()
            )
        return row(r)

    async def capability_update_call(self, owner, call_id, status, detail):
        await self.session.execute(
            text(
                "UPDATE workbench_capability_calls SET status=:status,detail=cast(:d as jsonb),updated_at=now() WHERE owner_actor_id=:o AND id=:id"
            ),
            {"o": owner, "id": call_id, "status": status, "d": json.dumps(detail)},
        )
