"""Attempt-scoped callbacks from the isolated instruction egress gateway."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench.instruction_requests import (
    InstructionRequests,
    gateway_binding,
    record_instruction_receipt,
)
from app.application.workbench.tokens import InstructionGateway, TokenIssuer
from app.domain.workbench.errors import Forbidden, WorkbenchError
from app.domain.workbench.pricing import Usage
from app.infrastructure.storage.postgres import get_db_session
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints.knowbot_runtime_routes import FormalService
from app.interfaces.endpoints.workbench_routes import _http, token_issuer
from app.interfaces.http.internal.host_guard import internal_host_only
from core.config import get_settings

router = APIRouter(
    prefix="/internal/v1/instruction-control",
    dependencies=[Depends(internal_host_only)],
)


def identity(
    issuer: Annotated[TokenIssuer | None, Depends(token_issuer)],
    authorization: Annotated[str | None, Header()] = None,
    attempt: Annotated[str | None, Header(alias="X-Instruction-Attempt")] = None,
    gateway: Annotated[str | None, Header(alias="X-Instruction-Gateway")] = None,
):
    scheme, _, token = (authorization or "").partition(" ")
    binding = (
        issuer.instruction_identity(token)
        if issuer and scheme.lower() == "bearer" and 0 < len(token) <= 4096
        else None
    )
    if (
        binding is None
        or gateway != str(binding.gateway)
        or attempt != str(binding.attempt)
    ):
        raise HTTPException(401, "Instruction gateway identity required")
    return binding


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_id: UUID
    provider: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=530)
    turn_id: str = Field(min_length=1, max_length=128)
    body: dict[str, Any]


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str = Field(min_length=1, max_length=128)
    response_id: str = Field(min_length=1, max_length=256)
    usage: dict[str, Any]


Binding = Annotated[InstructionGateway, Depends(identity)]


async def bound_quote(repo, binding):
    stored, quote = await gateway_binding(
        repo, binding.owner, binding.task, binding.attempt
    )
    if stored != binding:
        raise Forbidden("Instruction gateway differs from its Attempt")
    return quote


@router.post("/claim")
async def claim(binding: Binding, form: Claim, formal: FormalService):
    settings = get_settings()
    try:
        quote = await bound_quote(formal.repo, binding)
        if (
            form.credential_id != binding.credential
            or form.provider != quote["provider"]
            or form.endpoint != quote["base_url"] + "/responses"
        ):
            raise Forbidden("Loaded credential differs from the instruction ticket")
        route = next(
            (
                r
                for r in settings.workbench_instruction_routes
                if (r.provider, r.model, r.base_url)
                == (quote["provider"], quote["model"], quote["base_url"])
            ),
            None,
        )
        if route is None:
            raise Forbidden("Instruction route is not enabled")
        svc = InstructionRequests(
            formal,
            route,
            settings.workbench_model_prices(),
            default_base_url=settings.workbench_sandbox_provider_base_url,
        )
        url, body = await svc.claim(
            str(binding.owner),
            str(binding.task),
            str(binding.attempt),
            caller=quote["caller"],
            credential_id=str(form.credential_id),
            turn_id=form.turn_id,
            body=form.body,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return JSONResponse(
        {"url": url, "outgoing": body}, headers={"Cache-Control": "no-store"}
    )


@router.post("/receipt")
async def receipt(
    binding: Binding,
    form: Receipt,
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    repo = WorkbenchRepository(session)
    try:
        await bound_quote(repo, binding)
        usage = Usage.from_responses(form.usage)
        await record_instruction_receipt(
            repo,
            str(binding.owner),
            str(binding.task),
            str(binding.attempt),
            turn_id=form.turn_id,
            response_id=form.response_id,
            usage=usage,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, "Unsupported provider usage") from exc
    return JSONResponse({"recorded": True}, headers={"Cache-Control": "no-store"})
