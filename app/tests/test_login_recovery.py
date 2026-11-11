from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

import app.interfaces.http.web.auth as web_auth
from app.application.errors.exceptions import AppException


class FakeAuth:
    def __init__(
        self, error: AppException | None = None, logout_url: str | None = None
    ):
        self.error = error
        self.logout_url = logout_url
        self.ended: list[str | None] = []

    async def complete_login(self, **kwargs):
        raise self.error or AssertionError("unexpected callback")

    async def cancel_login(self, transaction_id):
        pass

    async def end_session(self, session_id):
        self.ended.append(session_id)
        return self.logout_url


def transport():
    app = FastAPI()
    # The real application installs this handler; the isolated router is sufficient
    # to exercise the HTTP/cookie/CSRF boundary without business infrastructure.
    from fastapi.responses import JSONResponse

    @app.exception_handler(AppException)
    async def error_handler(request, exc):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    app.include_router(web_auth.router, prefix="/api")
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        ("organization_not_allowed", 403, "organization_not_allowed"),
        ("provider_unavailable", 503, "provider_unavailable"),
        ("oidc_transaction_invalid", 401, "oidc_transaction_invalid"),
        ("token_invalid", 401, "token_invalid"),
        ("private_database_detail", 500, "auth_failed"),
    ],
)
async def test_callback_preserves_safe_failure_category(
    monkeypatch, code, status, reason
):
    monkeypatch.setattr(web_auth, "auth_service", FakeAuth(AppException(code, status)))
    async with httpx.AsyncClient(
        transport=transport(), base_url="https://testserver"
    ) as client:
        response = await client.get(
            "/api/auth/web/callback?code=test-code&state=test-state"
        )
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query) == {
        "error": ["auth_failed"],
        "reason": [reason],
    }
    assert response.headers["cache-control"] == "no-store"
    assert "test-code" not in response.headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "reason"),
    [
        ("access_denied", "authorization_denied"),
        ("login_required", "login_required"),
        ("server_error", "provider_unavailable"),
        ("private_provider_detail", "oidc_transaction_invalid"),
    ],
)
async def test_provider_error_is_not_always_reported_as_expiry(
    monkeypatch, provider_error, reason
):
    monkeypatch.setattr(web_auth, "auth_service", FakeAuth())
    async with httpx.AsyncClient(
        transport=transport(), base_url="https://testserver"
    ) as client:
        response = await client.get(
            "/api/auth/web/callback", params={"error": provider_error}
        )
    assert parse_qs(urlsplit(response.headers["location"]).query)["reason"] == [reason]


@pytest.mark.asyncio
async def test_explicit_account_switch_requires_origin_and_cookie_bound_csrf(
    monkeypatch,
):
    logout = "https://identity.example.test/api/logout?client_id=web"
    monkeypatch.setattr(web_auth.settings, "session_cookie_secure", True)
    fake = FakeAuth(logout_url=logout)
    monkeypatch.setattr(web_auth, "auth_service", fake)
    async with httpx.AsyncClient(
        transport=transport(), base_url="https://testserver"
    ) as client:
        # Merely preparing this action must not log out any account.
        prepared = await client.get("/api/auth/web/switch-account")
        assert prepared.headers["cache-control"] == "no-store"
        assert "HttpOnly" in prepared.headers["set-cookie"]
        assert not fake.ended
        token = prepared.json()["csrf_token"]
        origin = web_auth.profile.frontend_origins[0]
        for headers in (
            {},
            {"Origin": origin},
            {"Origin": "https://attacker.test", "X-CSRF-Token": token},
            {"Origin": origin, "X-CSRF-Token": "x" * 43},
        ):
            assert (
                await client.post("/api/auth/web/switch-account", headers=headers)
            ).status_code == 403
        assert not fake.ended
        response = await client.post(
            "/api/auth/web/switch-account",
            headers={
                "Origin": origin,
                "X-CSRF-Token": token,
            },
        )
        assert response.status_code == 200
        assert response.json() == {"logout_url": logout}
        assert fake.ended == [None]
        deleted = next(
            v
            for v in response.headers.get_list("set-cookie")
            if web_auth._switch_cookie + "=" in v
        )
        assert (
            "Secure" in deleted and "Max-Age=0" in deleted and "Domain=" not in deleted
        )
        # The response clears the temporary cookie, so it cannot be reused.
        assert (
            await client.post(
                "/api/auth/web/switch-account",
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": token,
                },
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_failed_identity_logout_does_not_claim_account_was_switched(monkeypatch):
    monkeypatch.setattr(web_auth, "auth_service", FakeAuth())
    async with httpx.AsyncClient(
        transport=transport(), base_url="https://testserver"
    ) as client:
        token = (await client.get("/api/auth/web/switch-account")).json()["csrf_token"]
        response = await client.post(
            "/api/auth/web/switch-account",
            headers={
                "Origin": web_auth.profile.frontend_origins[0],
                "X-CSRF-Token": token,
            },
        )
    assert response.status_code == 503
    assert "logout_url" not in response.json()
