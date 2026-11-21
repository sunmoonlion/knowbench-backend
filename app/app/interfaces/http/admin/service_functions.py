"""Read-only inspection of service revisions and the frozen methods pinning them."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workbench.capabilities import Strict
from app.domain.workbench.service_functions import IDENTIFIER, FunctionRevision
from app.infrastructure.storage.postgres import get_db_session
from app.infrastructure.workbench.service_functions import (
    function_page,
    read_revision,
    referencing_methods,
)
from app.interfaces.http.middleware.auth import require_knowbench_admin

router = APIRouter(
    prefix="/admin/v1/method-skill-center/functions",
    dependencies=[Depends(require_knowbench_admin)],
)
Session = Annotated[AsyncSession, Depends(get_db_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=2147483647)]


class Summary(Strict):
    provider: str
    name: str
    version: int
    digest: str
    description: str


class Functions(Strict):
    items: list[Summary]
    has_more: bool


class Method(Strict):
    method_id: UUID
    version: int
    digest: str
    title: str


class References(Strict):
    items: list[Method]
    has_more: bool


class Detail(Strict):
    revision: FunctionRevision
    references: References


@router.get("", response_model=Functions)
async def functions(
    response: Response,
    session: Session,
    q: Annotated[str, Query(max_length=160)] = "",
    limit: Limit = 25,
    offset: Offset = 0,
):
    response.headers["Cache-Control"] = "private, no-store"
    return await function_page(session, q.strip(), limit, offset)


@router.get("/{provider}/{name}/{version}", response_model=Detail)
async def detail(
    response: Response,
    session: Session,
    provider: Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,31}$")],
    name: Annotated[str, Path(pattern=IDENTIFIER)],
    version: Annotated[int, Path(ge=1, le=2147483647)],
    limit: Limit = 25,
    offset: Offset = 0,
):
    response.headers["Cache-Control"] = "private, no-store"
    rev = await read_revision(session, provider, name, version)
    return {
        "revision": rev,
        "references": await referencing_methods(session, rev, limit, offset),
    }
