"""Internal provider publication and reconciliation endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.service_functions import (
    IDENTIFIER,
    FunctionRevision,
    ServiceFunction,
)
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_function_publisher,
)
from app.infrastructure.storage.postgres import get_db_session
from app.infrastructure.workbench.service_functions import (
    read_revision,
    register_revision,
)
from app.interfaces.http.internal.host_guard import internal_host_only

router = APIRouter(
    prefix="/internal/v1/method-skill-center",
    dependencies=[Depends(internal_host_only)],
)
Publisher = Annotated[ServiceCaller, Depends(require_function_publisher)]
Session = Annotated[AsyncSession, Depends(get_db_session)]
Name = Annotated[str, Path(pattern=IDENTIFIER)]
Version = Annotated[int, Path(ge=1, le=2147483647)]


@router.put("/functions/{name}/{version}")
async def register(
    name: Name,
    version: Version,
    contract: ServiceFunction,
    publisher: Publisher,
    session: Session,
    response: Response,
) -> FunctionRevision:
    response.headers["Cache-Control"] = "no-store"
    return await register_revision(
        session, FunctionRevision.build(publisher.caller, name, version, contract)
    )


@router.get("/functions/{name}/{version}")
async def reconcile(
    name: Name,
    version: Version,
    publisher: Publisher,
    session: Session,
    response: Response,
) -> FunctionRevision:
    response.headers["Cache-Control"] = "no-store"
    return await read_revision(session, publisher.caller, name, version)
