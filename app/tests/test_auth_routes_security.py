from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import app.interfaces.http.admin.auth as admin_auth
import app.interfaces.http.middleware.auth as auth_middleware
import app.interfaces.http.web.auth as web_auth
from app.domain.security import BrowserSession, Principal
from app.main import app, settings


class FakeAuthService:
    def __init__(
        self, sessions: dict[str, BrowserSession], logout_url: str | None = None
    ) -> None:
        self.sessions = sessions
        self.deleted: list[str | None] = []
        self.logout_url = logout_url

    async def get_browser_session(
        self, session_id: str | None
    ) -> BrowserSession | None:
        return self.sessions.get(session_id or "")

    def validate_csrf(
        self,
        *,
        session: BrowserSession,
        method: str,
        origin: str | None,
        csrf_token: str | None,
    ) -> None:
        profile = settings.browser_profile(session.principal.surface)  # type: ignore[arg-type]
        if method not in {"GET", "HEAD", "OPTIONS"} and (
            origin not in profile.frontend_origins or csrf_token != session.csrf_token
        ):
            from app.application.errors.exceptions import ForbiddenError

            raise ForbiddenError("CSRF validation failed")

    @staticmethod
    def require_scopes(
        principal: Principal, required: set[str] | frozenset[str]
    ) -> None:
        if not principal.has_scopes(required):
            from app.application.errors.exceptions import ForbiddenError

            raise ForbiddenError("Required scope missing")

    async def delete_session(self, session_id: str | None) -> None:
        self.deleted.append(session_id)

    async def end_session(self, session_id: str | None) -> str | None:
        self.deleted.append(session_id)
        return self.logout_url


def session(surface: str, *scopes: str) -> BrowserSession:
    now = datetime.now(UTC)
    profile = settings.browser_profile(surface)  # type: ignore[arg-type]
    return BrowserSession(
        principal=Principal(
            actor_type="user",
            subject="user-123",
            issuer="https://identity.example.test",
            app="knowbench",
            surface=surface,  # type: ignore[arg-type]
            audience=profile.client_id or f"{surface}-client",
            actor_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
            display_name="Test User",
            email="user@example.test",
            roles=("editor",),
            scopes=frozenset(scopes),
            authenticated_at=now,
            expires_at=now + timedelta(minutes=10),
            policy_version=profile.policy_version,
        ),
        csrf_token="csrf-token-with-at-least-thirty-two-characters",
    )


@pytest.mark.asyncio
async def test_admin_and_web_me_use_distinct_cookies_and_minimal_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_admin = FakeAuthService({"admin-session": session("admin", "knowbench:admin")})
    fake_web = FakeAuthService({"web-session": session("web", "profile:read")})
    monkeypatch.setattr(auth_middleware, "admin_auth_service", fake_admin)
    monkeypatch.setattr(auth_middleware, "web_auth_service", fake_web)
    monkeypatch.setattr(admin_auth, "auth_service", fake_admin)
    monkeypatch.setattr(web_auth, "auth_service", fake_web)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/auth/admin/me")).status_code == 401
        client.cookies.set("sunmoonai_knowbench_admin_sid", "admin-session")
        admin_response = await client.get("/api/auth/admin/me")
        assert admin_response.status_code == 200
        assert admin_response.json()["user"]["surface"] == "admin"

        # An Admin cookie must not authorize the Web surface.
        assert (await client.get("/api/auth/web/me")).status_code == 401
        client.cookies.set("sunmoonai_knowbench_web_sid", "web-session")
        web_response = await client.get("/api/auth/web/me")
        assert web_response.status_code == 200
        assert web_response.json()["user"]["surface"] == "web"
        rendered = str(web_response.json())
        assert "subject" not in rendered
        assert "audience" not in rendered
        assert "access_token" not in rendered
        assert "id_token" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("roles", "scopes", "allowlist", "allowed"),
    [
        (("admin",), (), {"admin", "operator"}, True),
        (("operator",), (), {"admin", "operator"}, True),
        (("editor",), (), {"admin", "operator"}, False),
        ((), ("knowbench:admin",), {"admin", "operator"}, True),
        (("admin",), (), set(), False),
    ],
)
async def test_admin_business_routes_accept_the_admin_roles(
    monkeypatch: pytest.MonkeyPatch,
    roles: tuple[str, ...],
    scopes: tuple[str, ...],
    allowlist: set[str],
    allowed: bool,
) -> None:
    """只给角色、不发「应用名:admin」：白名单里的管理角色能用管理接口（账 134）。"""
    base = session("admin", *scopes)
    principal = base.principal.model_copy(update={"roles": roles})
    fake = FakeAuthService({"s": base.model_copy(update={"principal": principal})})
    monkeypatch.setattr(auth_middleware, "admin_auth_service", fake)
    monkeypatch.setattr(
        auth_middleware,
        "_admin_profile",
        dataclasses.replace(
            auth_middleware._admin_profile, role_allowlist=frozenset(allowlist)
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        client.cookies.set("sunmoonai_knowbench_admin_sid", "s")
        response = await client.post(
            "/api/admin/v1/diagnostics/tasks/ping",
            headers={
                "Origin": settings.browser_profile("admin").frontend_origins[0],
                "X-CSRF-Token": base.csrf_token,
            },
        )
    # 过了身份这一关就轮到任务队列（测试里没配，503）；没过是 403
    assert response.status_code == (503 if allowed else 403), response.text


@pytest.mark.parametrize(
    ("roles", "scopes", "granted"),
    [
        (("admin",), (), None),  # 全部
        (("operator",), (), {"knowbench:admin"}),  # 只有基本权限
        (("operator",), ("knowbench:release",), {"knowbench:admin", "knowbench:release"}),
        (("editor",), (), set()),
        ((), ("knowbench:admin",), {"knowbench:admin"}),
    ],
)
def test_admin_roles_become_scopes_and_only_admin_gets_everything(
    monkeypatch: pytest.MonkeyPatch,
    roles: tuple[str, ...],
    scopes: tuple[str, ...],
    granted: set[str] | None,
) -> None:
    """operator 不等于 admin（审读第 4 条）：要多一项权限的（交付、退回）它过不了。"""
    monkeypatch.setattr(
        auth_middleware,
        "_admin_profile",
        dataclasses.replace(
            auth_middleware._admin_profile,
            role_allowlist=frozenset({"admin", "operator"}),
        ),
    )
    principal = session("admin", *scopes).principal.model_copy(update={"roles": roles})
    result = auth_middleware.granted_scopes(principal)
    assert (None if result is None else set(result)) == granted


def test_admin_has_no_signup_and_web_has_signup_and_continue() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/auth/admin/signup" not in paths
    assert "/api/auth/web/signup" in paths
    assert "/api/auth/web/continue" in paths


@pytest.mark.asyncio
async def test_logout_is_post_only_and_surface_csrf_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAuthService({"member": session("web", "profile:read")})
    monkeypatch.setattr(auth_middleware, "web_auth_service", fake)
    monkeypatch.setattr(web_auth, "auth_service", fake)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        client.cookies.set("sunmoonai_knowbench_web_sid", "member")
        assert (await client.get("/api/auth/web/logout")).status_code == 405
        assert (await client.post("/api/auth/web/logout")).status_code == 403
        allowed = await client.post(
            "/api/auth/web/logout",
            headers={
                "Origin": "http://localhost:3000",
                "X-CSRF-Token": "csrf-token-with-at-least-thirty-two-characters",
            },
        )
        assert allowed.status_code == 204
        assert fake.deleted == ["member"]
        assert allowed.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "cookie", "origin", "module"),
    [
        ("web", "sunmoonai_knowbench_web_sid", "http://localhost:3000", web_auth),
        ("admin", "sunmoonai_knowbench_admin_sid", "http://localhost:5173", admin_auth),
    ],
)
async def test_logout_hands_back_the_provider_logout_url(
    surface: str,
    cookie: str,
    origin: str,
    module: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://identity.example.test/api/logout?client_id=x"
    fake = FakeAuthService({"member": session(surface, "profile:read")}, url)
    monkeypatch.setattr(auth_middleware, f"{surface}_auth_service", fake)
    monkeypatch.setattr(module, "auth_service", fake)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        client.cookies.set(cookie, "member")
        response = await client.post(
            f"/api/auth/{surface}/logout",
            headers={
                "Origin": origin,
                "X-CSRF-Token": "csrf-token-with-at-least-thirty-two-characters",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"logout_url": url}
    assert response.headers["cache-control"] == "no-store"
    assert fake.deleted == ["member"]
    assert cookie in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["web", "admin"])
async def test_logged_out_returns_to_the_frontend(surface: str) -> None:
    profile = settings.browser_profile(surface)  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/auth/{surface}/logged-out")
    assert response.status_code == 302
    assert response.headers["location"].startswith(profile.frontend_base_url)
    assert response.headers["location"].endswith(profile.default_return_to)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_health_aliases_are_public_and_equivalent() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        live_responses = [
            await client.get(path) for path in ("/health/live", "/health")
        ]
        ready_responses = [
            await client.get(path)
            for path in ("/health/ready", "/ready", "/api/health")
        ]
    assert all(response.status_code == 200 for response in live_responses)
    assert {str(response.json()) for response in live_responses} == {"{'status': 'ok'}"}
    assert all(response.status_code == 503 for response in ready_responses)
    assert {str(response.json()) for response in ready_responses} == {
        "{'status': 'not_ready'}"
    }
    assert all(
        response.headers["x-content-type-options"] == "nosniff"
        for response in (*live_responses, *ready_responses)
    )


@pytest.mark.asyncio
async def test_unknown_api_returns_stable_error_envelope() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/not-found")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["operation_id"]
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404
    assert response.json()["code"] == "not_found"
    assert response.json()["instance"] == "/api/not-found"
