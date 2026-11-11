from __future__ import annotations

import logging
import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, Header, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.application.errors.exceptions import (
    AppException,
    ForbiddenError,
    ServiceUnavailableError,
)
from app.bootstrap.auth import build_auth_service
from app.domain.security import BrowserSession
from app.interfaces.http.auth_common import (
    frontend_redirect,
    set_browser_cookie,
    set_session_cookie,
)
from app.interfaces.http.middleware.auth import get_web_browser_session
from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
profile = settings.browser_profile("web")
router = APIRouter(prefix="/auth/web", tags=["Web authentication"])
auth_service = build_auth_service("web", settings)


def _login_error_redirect(reason: str) -> str:
    query = urlencode({"error": "auth_failed", "reason": reason})
    return frontend_redirect(
        profile, f"/{settings.web_frontend_default_locale}/login?{query}"
    )


async def _start(return_to: str | None, mode: str) -> RedirectResponse:
    start = await auth_service.begin_login(return_to, mode)
    response = RedirectResponse(url=start.authorization_url, status_code=302)
    set_browser_cookie(
        response,
        settings=settings,
        key=profile.transaction_cookie_name,
        value=start.transaction_id,
        max_age=settings.auth_transaction_ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/login", summary="Start Web OIDC login")
async def login(return_to: str | None = Query(default=None)) -> RedirectResponse:
    return await _start(return_to, "login")


# A failed login has no local session/CSRF token yet. Issue a short-lived,
# host-only double-submit token for this explicit account-switch action.
_switch_cookie = (
    ("__Host-" if settings.auth_cookie_secure else "")
    + profile.transaction_cookie_name
    + "_switch"
)


@router.get("/switch-account", summary="Prepare explicit account switch")
async def prepare_account_switch() -> JSONResponse:
    token = secrets.token_urlsafe(32)
    response = JSONResponse({"csrf_token": token})
    set_browser_cookie(
        response, settings=settings, key=_switch_cookie, value=token, max_age=300
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/switch-account", summary="End current identity before signing in again")
async def switch_account(
    origin: str | None = Header(default=None),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    switch_token: str | None = Cookie(default=None, alias=_switch_cookie),
    session_id: str | None = Cookie(default=None, alias=profile.session_cookie_name),
) -> JSONResponse:
    if (
        origin not in profile.frontend_origins
        or not csrf_token
        or not switch_token
        or not csrf_token.isascii()
        or not switch_token.isascii()
        or len(csrf_token) != 43
        or len(switch_token) != 43
        or not secrets.compare_digest(csrf_token, switch_token)
    ):
        raise ForbiddenError("CSRF validation failed")
    logout_url = await auth_service.end_session(session_id)
    if not logout_url:
        raise ServiceUnavailableError("Identity sign-out is unavailable")
    response = JSONResponse({"logout_url": logout_url})
    response.delete_cookie(
        _switch_cookie,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(profile.session_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/signup", summary="Start Web OIDC signup")
async def signup(return_to: str | None = Query(default=None)) -> RedirectResponse:
    return await _start(return_to, "signup")


@router.get("/callback", summary="Complete Web OIDC login")
async def callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    provider_error: str | None = Query(default=None, alias="error"),
    transaction_id: str | None = Cookie(
        default=None, alias=profile.transaction_cookie_name
    ),
) -> RedirectResponse:
    if provider_error or not code or not state:
        await auth_service.cancel_login(transaction_id)
        reason = {
            "access_denied": "authorization_denied",
            "login_required": "login_required",
            "interaction_required": "login_required",
            "consent_required": "authorization_denied",
            "server_error": "provider_unavailable",
            "temporarily_unavailable": "provider_unavailable",
        }.get(provider_error or "", "oidc_transaction_invalid")
        response = RedirectResponse(url=_login_error_redirect(reason), status_code=302)
        response.delete_cookie(profile.transaction_cookie_name, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
    try:
        session, return_to = await auth_service.complete_login(
            code=code, state=state, transaction_id=transaction_id
        )
    except AppException as exc:
        allowed_reasons = {
            "oidc_transaction_invalid",
            "token_invalid",
            "issuer_mismatch",
            "audience_mismatch",
            "provider_unavailable",
            "organization_not_allowed",
        }
        reason = exc.code if exc.code in allowed_reasons else "auth_failed"
        logger.warning(
            "web_oidc_callback_rejected status=%s reason=%s",
            exc.status_code,
            reason,
        )
        response = RedirectResponse(url=_login_error_redirect(reason), status_code=302)
        response.delete_cookie(profile.transaction_cookie_name, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
    response = RedirectResponse(
        url=frontend_redirect(profile, return_to), status_code=302
    )
    set_session_cookie(
        response,
        settings=settings,
        profile=profile,
        session=session,
        session_id=session.session_id,
        expires_at=session.expires_at,
    )
    return response


@router.get("/continue", summary="Continue to Web application")
async def continue_to_app(
    session_id: str | None = Cookie(default=None, alias=profile.session_cookie_name),
) -> RedirectResponse:
    session = await auth_service.get_browser_session(session_id)
    target = (
        profile.default_return_to
        if session is not None
        else f"/{settings.web_frontend_default_locale}/login"
    )
    response = RedirectResponse(url=frontend_redirect(profile, target), status_code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(
    "/logout",
    summary="Log out Web browser",
    responses={
        200: {
            "description": "Logged out; navigate to logout_url to end single sign-on"
        },
        204: {"description": "Logged out locally"},
    },
)
async def logout(
    _: Annotated[BrowserSession, Depends(get_web_browser_session)],
    session_id: str | None = Cookie(default=None, alias=profile.session_cookie_name),
) -> Response:
    logout_url = await auth_service.end_session(session_id)
    response: Response = (
        JSONResponse({"logout_url": logout_url})
        if logout_url
        else Response(status_code=204)
    )
    response.delete_cookie(profile.session_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/logged-out", summary="Return from provider logout")
async def logged_out() -> RedirectResponse:
    """The provider redirects here after ending its session; on to the Web app."""
    response = RedirectResponse(
        url=frontend_redirect(profile, profile.default_return_to), status_code=302
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/me", summary="Get current Web user")
async def me(
    session: Annotated[BrowserSession, Depends(get_web_browser_session)],
) -> dict[str, object]:
    principal = session.principal
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
        "csrf_token": session.csrf_token,
    }
