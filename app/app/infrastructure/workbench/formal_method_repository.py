"""Admission run storage and projections of KnowLab-owned frozen versions."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.errors import IdempotencyConflict


class FormalMethodRepositoryMixin:
    session: AsyncSession

    async def incubation_asset(self, owner, reference):
        return await self.session.scalar(
            text("""SELECT asset_id FROM workbench_incubation_tool_bindings
        WHERE owner_actor_id=:owner AND method_id=:method_id AND version=:version AND digest=:digest"""),
            {"owner": owner, **reference.model_dump()},
        )

    async def select_incubation_asset(self, owner, reference, asset_id):
        await self.session.execute(
            text("""INSERT INTO workbench_incubation_tool_bindings
        (owner_actor_id,method_id,version,digest,asset_id) VALUES(:owner,:method_id,:version,:digest,:asset)
        ON CONFLICT(owner_actor_id,method_id,version) DO UPDATE SET asset_id=EXCLUDED.asset_id
        WHERE workbench_incubation_tool_bindings.digest=EXCLUDED.digest"""),
            {"owner": owner, **reference.model_dump(), "asset": asset_id},
        )

    async def list_formal_methods(self, owner):
        rows = (
            (
                await self.session.execute(
                    text("""SELECT * FROM workbench_formal_methods WHERE owner_actor_id=:owner
              ORDER BY created_at DESC,method_id,version DESC"""),
                    {"owner": owner},
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def list_formal_session_calls(self, owner, session_id, limit=50):
        from app.infrastructure.workbench.capability_repository import row

        if not 1 <= limit <= 50:
            raise ValueError("Formal call history limit must be between 1 and 50")
        rows = (
            (
                await self.session.execute(
                    text("""SELECT * FROM workbench_calls
              WHERE owner_actor_id=:owner AND session_id=:session_id
                AND kind='method' AND origin='method' AND call_key LIKE 'formal-session:%'
                AND parent_call_id IS NULL
              ORDER BY created_at DESC,id DESC LIMIT :limit"""),
                    {"owner": owner, "session_id": session_id, "limit": limit},
                )
            )
            .mappings()
            .all()
        )
        return [row(value) for value in rows]

    async def lock_formal_execution(self, run_id: str):
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 45))"),
            {"key": run_id},
        )

    async def seal_formal_execution(
        self, owner: str, run_id: str, result: dict, evidence: dict
    ):
        """Caller holds the admission/run locks; evidence survives task cleanup."""
        prior = await self.formal_validation(owner, run_id, lock=True)
        if prior is None:
            raise IdempotencyConflict("Formal validation disappeared")
        if prior.get("execution_evidence") is not None:
            if prior["execution_evidence"] != evidence or prior["result"] != result:
                raise IdempotencyConflict("Formal execution evidence is immutable")
            return
        await self.session.execute(
            text("""UPDATE workbench_formal_validations
              SET result=cast(:result as jsonb), execution_evidence=cast(:evidence as jsonb), updated_at=now()
              WHERE run_id=:run_id AND owner_actor_id=:owner"""),
            {
                "owner": owner,
                "run_id": run_id,
                "result": json.dumps(result),
                "evidence": json.dumps(evidence),
            },
        )

    async def formal_validation(self, owner: str, run_id: str, *, lock=False):
        row = (
            (
                await self.session.execute(
                    text(
                        "SELECT * FROM workbench_formal_validations "
                        "WHERE owner_actor_id=:o AND run_id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"o": owner, "id": run_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def insert_formal_validation(self, record: dict[str, Any]):
        row = (
            (
                await self.session.execute(
                    text("""
            INSERT INTO workbench_formal_validations
              (run_id,owner_actor_id,caller,request_id,request_digest,request,result)
            VALUES (:run_id,:owner_actor_id,:caller,:request_id,:request_digest,
              cast(:request as jsonb),cast(:result as jsonb))
            ON CONFLICT DO NOTHING RETURNING *
        """),
                    {
                        **record,
                        "request": json.dumps(record["request"]),
                        "result": json.dumps(record["result"]),
                    },
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row)
        existing = (
            (
                await self.session.execute(
                    text("""
            SELECT * FROM workbench_formal_validations WHERE owner_actor_id=:o
              AND run_id=:id
        """),
                    {
                        "o": record["owner_actor_id"],
                        "id": record["run_id"],
                        "request_id": record["request_id"],
                    },
                )
            )
            .mappings()
            .all()
        )
        if (
            len(existing) != 1
            or str(existing[0]["run_id"]) != record["run_id"]
            or existing[0]["caller"] != record["caller"]
            or existing[0]["request_digest"] != record["request_digest"]
        ):
            raise IdempotencyConflict("Validation identity was used with other content")
        return dict(existing[0])

    async def formal_method(self, owner: str, method_id: str, version: int):
        row = (
            (
                await self.session.execute(
                    text("""
            SELECT * FROM workbench_formal_methods WHERE owner_actor_id=:o
              AND method_id=:id AND version=:v
        """),
                    {"o": owner, "id": method_id, "v": version},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def insert_formal_method(self, record: dict[str, Any]):
        row = (
            (
                await self.session.execute(
                    text("""
            INSERT INTO workbench_formal_methods
              (method_id,version,owner_actor_id,caller,digest,document,run_id)
            VALUES (:method_id,:version,:owner_actor_id,:caller,:digest,
              cast(:document as jsonb),:run_id)
            ON CONFLICT DO NOTHING RETURNING *
        """),
                    {**record, "document": json.dumps(record["document"])},
                )
            )
            .mappings()
            .first()
        )
        if row:
            return dict(row)
        prior = await self.formal_method(
            record["owner_actor_id"], record["method_id"], record["version"]
        )
        if (
            prior is None
            or prior["caller"] != record["caller"]
            or prior["document"] != record["document"]
        ):
            raise IdempotencyConflict("Frozen method identity is immutable")
        return prior
