"""Provider registrations. The primary key serializes concurrent first writers."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.errors.exceptions import ConcurrencyConflictError, NotFoundError
from app.domain.workbench.service_functions import FunctionRevision, ServiceFunction


async def read_revision(session: AsyncSession, provider: str, name: str, version: int):
    result = await session.execute(
        text("""
            SELECT digest, contract FROM workbench_registry_service_functions
            WHERE provider=:provider AND name=:name AND version=:version
        """),
        dict(provider=provider, name=name, version=version),
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise NotFoundError()
    return checked_revision(dict(row, provider=provider, name=name, version=version))


def checked_revision(row):
    revision = FunctionRevision.build(
        row["provider"],
        row["name"],
        row["version"],
        ServiceFunction.model_validate(row["contract"]),
    )
    if revision.digest != row["digest"]:
        raise ConcurrencyConflictError(code="service_function_digest_mismatch")
    return revision


async def register_revision(session: AsyncSession, proposed: FunctionRevision):
    async with session.begin():
        await session.execute(
            text("""
            INSERT INTO workbench_registry_service_functions (provider,name,version,digest,contract)
            VALUES (:provider,:name,:version,:digest,CAST(:contract AS jsonb))
            ON CONFLICT (provider,name,version) DO NOTHING
        """),
            {
                **proposed.model_dump(exclude={"contract"}),
                "contract": proposed.contract.model_dump_json(),
            },
        )
        stored = await read_revision(
            session, proposed.provider, proposed.name, proposed.version
        )
        if stored.digest != proposed.digest:
            raise ConcurrencyConflictError(
                "This revision already has a different contract",
                code="service_function_version_conflict",
            )
        return stored


async def function_page(session, query, limit, offset):
    result = await session.execute(
        text("""
        SELECT provider, name, version, digest, contract->>'description' AS description
        FROM workbench_registry_service_functions
        WHERE strpos(lower(provider || '/' || name), lower(:query)) > 0 OR digest=:query
        ORDER BY provider, name, version DESC LIMIT :limit OFFSET :offset
    """),
        {"query": query, "limit": limit + 1, "offset": offset},
    )
    rows = result.mappings().all()
    return {"items": rows[:limit], "has_more": len(rows) > limit}


async def referencing_methods(session, revision, limit, offset):
    result = await session.execute(
        text("""
        SELECT method_id, version, digest,
          document #>> '{draft,definition,capability_declaration,title}' AS title
        FROM workbench_formal_methods
        WHERE caller='knowlab' AND document #> '{draft,source}' @> CAST(:pin AS jsonb)
        ORDER BY method_id, version DESC LIMIT :limit OFFSET :offset
    """),
        {
            "pin": json.dumps(
                {
                    "kind": "service_function",
                    "source": revision.provider,
                    "entry": revision.name,
                    "version": str(revision.version),
                    "digest": revision.digest,
                }
            ),
            "limit": limit + 1,
            "offset": offset,
        },
    )
    rows = result.mappings().all()
    return {"items": rows[:limit], "has_more": len(rows) > limit}
