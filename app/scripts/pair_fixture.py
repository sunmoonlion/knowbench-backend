"""Controlled dual-surface fixture for fast frontend contract feedback.

This process is deliberately not imported by the production bootstrap.  It
keeps local Playwright tests deterministic; the Architecture v2 container/KIND
gate still has to pair both frontends with the real Backend bootstrap.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Cookie, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from app.application.dto.interaction import (
    CancelRunRequest,
    CreateRunRequest,
    RunAction,
)
from app.application.ports import InteractionContext
from app.application.services.web_interaction import (
    REFERENCE_EVIDENCE_ID,
    REFERENCE_RUN_ID,
    ReferenceWebInteractionAdapter,
)
from app.domain.security import Principal

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_origin = os.environ.get("PAIR_ORIGIN", "http://127.0.0.1:3009")
_csrf = "csrf-token-value-that-is-long-enough-1234"
_adapter = ReferenceWebInteractionAdapter()


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": code,
                "operation_id": "pair-fixture-operation",
            }
        },
    )


def _principal(surface: str, *, operator: bool = False) -> Principal:
    now = datetime.now(UTC)
    roles = ("operator",) if operator else (
        ("admin",) if surface == "admin" else ("member",)
    )
    return Principal(
        actor_type="user",
        subject=f"pair-{surface}-subject",
        issuer="https://identity.example.test",
        app="knowbench",
        surface=surface,  # type: ignore[arg-type]
        audience=f"tpl-{surface}-pair",
        actor_id=UUID("b42cf3bb-d63e-5df5-a884-9c34286f2608"),
        display_name="Paired E2E User",
        email=f"{surface}@example.test",
        roles=roles,
        scopes=frozenset({"knowbench:admin"}) if surface == "admin" else frozenset(),
        authenticated_at=now,
        expires_at=now + timedelta(hours=1),
        policy_version=f"tpl-{surface}-v2",
    )


def _session_payload(principal: Principal) -> dict[str, object]:
    return {
        "contract_version": 1,
        "authenticated": True,
        "user": {
            "actor_id": str(principal.actor_id),
            "app": principal.app,
            "surface": principal.surface,
            "display_name": principal.display_name,
            "email": principal.email,
            "roles": list(principal.roles),
            "scopes": sorted(principal.scopes),
            "expires_at": principal.expires_at.isoformat(),
        },
        "csrf_token": _csrf,
    }


def _web_context() -> InteractionContext:
    return InteractionContext(
        principal=_principal("web"), operation_id="pair-fixture-operation"
    )


def _csrf_allowed(request: Request, token: str | None) -> bool:
    return request.headers.get("origin") == _origin and token == _csrf


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "surface": "unified-backend"}


@app.get("/api/auth/admin/me", response_model=None)
async def admin_me(
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_admin_sid"),
) -> Response | dict[str, object]:
    if session_id not in {"e2e-session", "operator-session"}:
        return _error(401, "auth_required")
    return _session_payload(
        _principal("admin", operator=session_id == "operator-session")
    )


@app.get("/api/auth/web/me", response_model=None)
async def web_me(
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
) -> Response | dict[str, object]:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    return _session_payload(_principal("web"))


@app.post("/api/web/v1/runs", response_model=None)
async def create_run(
    command: CreateRunRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response | dict[str, object]:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    if not _csrf_allowed(request, csrf_token):
        return _error(403, "forbidden")
    snapshot = await _adapter.create_run(command, _web_context())
    return snapshot.model_dump(mode="json")


@app.post("/api/auth/{surface}/logout", status_code=204)
async def logout(
    surface: str,
    request: Request,
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response:
    if surface not in {"admin", "web"} or not _csrf_allowed(request, csrf_token):
        return _error(403, "forbidden")
    response = Response(status_code=204)
    response.delete_cookie(f"sunmoonai_knowbench_{surface}_sid", path="/")
    return response


@app.get("/api/web/v1/runs/{run_id}", response_model=None)
async def get_run(
    run_id: str,
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
) -> Response | dict[str, object]:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    snapshot = await _adapter.get_run(run_id, _web_context())
    return snapshot.model_dump(mode="json")


@app.get("/api/web/v1/runs/{run_id}/events")
async def stream_run(
    run_id: str,
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
) -> Response:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    stream = await _adapter.open_run_stream(run_id, None, _web_context())

    async def events():
        async for event in stream:
            yield (
                f"id: {event.event_id}\n"
                "event: run-event\n"
                f"data: {event.model_dump_json()}\n\n"
            )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/web/v1/runs/{run_id}/actions", response_model=None)
async def submit_action(
    run_id: str,
    command: RunAction,
    request: Request,
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response | dict[str, object]:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    if not _csrf_allowed(request, csrf_token):
        return _error(403, "forbidden")
    snapshot = await _adapter.submit_action(run_id, command, _web_context())
    return snapshot.model_dump(mode="json")


@app.post("/api/web/v1/runs/{run_id}/cancel", response_model=None)
async def cancel_run(
    run_id: str,
    command: CancelRunRequest,
    request: Request,
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response | dict[str, object]:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    if not _csrf_allowed(request, csrf_token):
        return _error(403, "forbidden")
    snapshot = await _adapter.cancel_run(run_id, command, _web_context())
    return snapshot.model_dump(mode="json")


@app.get("/api/web/v1/citations/{evidence_id}/source")
async def citation_source(
    evidence_id: str,
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
) -> Response:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    resolution = await _adapter.resolve_citation_source(evidence_id, _web_context())
    return RedirectResponse(resolution.location, status_code=302)


@app.get("/api/web/v1/reference/sources/{evidence_id}", response_model=None)
async def reference_source(
    evidence_id: str,
    session_id: str | None = Cookie(default=None, alias="sunmoonai_knowbench_web_sid"),
) -> Response | dict[str, str]:
    if session_id != "e2e-session":
        return _error(401, "auth_required")
    if evidence_id != str(REFERENCE_EVIDENCE_ID):
        return _error(403, "resource_forbidden")
    return {"source": "authorized-reference-fixture", "evidence_id": evidence_id}


assert str(REFERENCE_RUN_ID) == "00000000-0000-5000-8000-000000000001"
