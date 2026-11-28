from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import ValidationError as PydanticValidationError

from app.application.audit_context import get_context
from app.application.dto.interaction import (
    RUN_EVENT_ADAPTER,
    CancelRunRequest,
    CreateRunRequest,
    RunAction,
    RunSnapshot,
)
from app.application.errors.exceptions import (
    BadGatewayError,
    BadRequestError,
    ForbiddenError,
)
from app.application.ports import InteractionContext, WebInteractionPort
from app.application.services.web_interaction import (
    REFERENCE_EVIDENCE_ID,
    get_web_interaction_port,
)
from app.domain.security import Principal
from app.interfaces.http.middleware.auth import get_web_current_user
from core.config import get_settings

router = APIRouter(prefix="/web/v1", tags=["Web interaction"])


def _context(principal: Principal) -> InteractionContext:
    audit = get_context()
    return InteractionContext(
        principal=principal,
        operation_id=(
            audit.operation_id
            if audit is not None and audit.operation_id
            else "operation-unavailable"
        ),
    )


def _uuid(value: str, *, code: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise BadRequestError("The resource identifier is invalid", code=code) from exc
    if parsed.version not in {4, 5}:
        raise BadRequestError("The resource identifier is invalid", code=code)
    return str(parsed)


def _snapshot(candidate: object, expected_id: str | None = None) -> RunSnapshot:
    try:
        snapshot = RunSnapshot.model_validate(candidate)
    except PydanticValidationError as exc:
        raise BadGatewayError() from exc
    if expected_id is not None and snapshot.run_id != UUID(expected_id):
        raise BadGatewayError()
    return snapshot


@router.post("/runs", response_model=RunSnapshot)
async def create_run(
    command: CreateRunRequest,
    principal: Annotated[Principal, Depends(get_web_current_user)],
    interaction: Annotated[WebInteractionPort, Depends(get_web_interaction_port)],
) -> RunSnapshot:
    return _snapshot(await interaction.create_run(command, _context(principal)))


@router.get("/runs/{run_id}", response_model=RunSnapshot)
async def get_run(
    run_id: str,
    principal: Annotated[Principal, Depends(get_web_current_user)],
    interaction: Annotated[WebInteractionPort, Depends(get_web_interaction_port)],
) -> RunSnapshot:
    normalized_run_id = _uuid(run_id, code="resource_invalid")
    return _snapshot(
        await interaction.get_run(
            normalized_run_id, _context(principal)
        ),
        normalized_run_id,
    )


@router.get("/runs/{run_id}/events")
async def stream_run(
    run_id: str,
    principal: Annotated[Principal, Depends(get_web_current_user)],
    interaction: Annotated[WebInteractionPort, Depends(get_web_interaction_port)],
    header_cursor: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    query_cursor: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    if header_cursor and query_cursor and header_cursor != query_cursor:
        raise BadRequestError(
            "Conflicting event cursors were supplied", code="cursor_invalid"
        )
    cursor = header_cursor or query_cursor
    if cursor:
        cursor = _uuid(cursor, code="cursor_invalid")
    normalized_run_id = _uuid(run_id, code="resource_invalid")
    stream = await interaction.open_run_stream(
        normalized_run_id, cursor, _context(principal)
    )

    async def events() -> AsyncIterator[str]:
        async for candidate in stream:
            event = RUN_EVENT_ADAPTER.validate_python(candidate)
            payload = json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"id: {event.event_id}\nevent: run-event\ndata: {payload}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/actions", response_model=RunSnapshot)
async def submit_action(
    run_id: str,
    command: RunAction,
    principal: Annotated[Principal, Depends(get_web_current_user)],
    interaction: Annotated[WebInteractionPort, Depends(get_web_interaction_port)],
) -> RunSnapshot:
    normalized_run_id = _uuid(run_id, code="resource_invalid")
    return _snapshot(
        await interaction.submit_action(
            normalized_run_id, command, _context(principal)
        ),
        normalized_run_id,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunSnapshot)
async def cancel_run(
    run_id: str,
    command: CancelRunRequest,
    principal: Annotated[Principal, Depends(get_web_current_user)],
    interaction: Annotated[WebInteractionPort, Depends(get_web_interaction_port)],
) -> RunSnapshot:
    normalized_run_id = _uuid(run_id, code="resource_invalid")
    return _snapshot(
        await interaction.cancel_run(
            normalized_run_id,
            command,
            _context(principal),
        ),
        normalized_run_id,
    )


@router.get("/citations/{evidence_id}/source")
async def citation_source(
    evidence_id: str,
    principal: Annotated[Principal, Depends(get_web_current_user)],
    interaction: Annotated[WebInteractionPort, Depends(get_web_interaction_port)],
) -> RedirectResponse:
    resolution = await interaction.resolve_citation_source(
        _uuid(evidence_id, code="resource_invalid"), _context(principal)
    )
    location = resolution.location
    if (
        not location.startswith("/")
        or location.startswith("//")
        or "\\" in location
        or "\x00" in location
    ):
        raise BadRequestError(
            "The citation source could not be resolved", code="source_invalid"
        )
    response = RedirectResponse(url=location, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/reference/sources/{evidence_id}", include_in_schema=False)
async def reference_source(
    evidence_id: str,
    principal: Annotated[Principal, Depends(get_web_current_user)],
) -> dict[str, str]:
    settings = get_settings()
    if (
        not settings.reference_interaction_enabled
        or principal.app != settings.app_slug
        or principal.surface != "web"
        or _uuid(evidence_id, code="resource_invalid") != str(REFERENCE_EVIDENCE_ID)
    ):
        raise ForbiddenError(
            "The requested citation is not available", code="resource_forbidden"
        )
    return {
        "source": "authorized-reference-fixture",
        "evidence_id": str(REFERENCE_EVIDENCE_ID),
    }
