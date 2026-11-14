"""Admission run storage and projections of KnowLab-owned frozen versions."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.errors import IdempotencyConflict


class FormalMethodRepositoryMixin:
    session: AsyncSession

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
