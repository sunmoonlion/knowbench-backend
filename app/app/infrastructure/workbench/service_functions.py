"""Provider registrations. The primary key serializes concurrent first writers."""

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
    revision = FunctionRevision.build(
        provider, name, version, ServiceFunction.model_validate(row["contract"])
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
