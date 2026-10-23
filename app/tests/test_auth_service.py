from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

import app.infrastructure.storage.session_store as session_store_module
from app.application.errors.exceptions import (
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.application.services.auth_service import AuthService
from app.domain.security import BrowserSession, Principal
from app.infrastructure.storage.session_store import RedisSessionStore
from core.config import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **kwargs: object) -> bool:
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


class FakeRedisHolder:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client


class FakeOidc:
    async def build_authorization_url(self, **values: str) -> str:
        return f"https://identity.example.test/authorize?state={values['state']}"

    async def exchange_authorization_code(self, **_: str) -> dict[str, object]:
        now = int(datetime.now(UTC).timestamp())
        return {
            "iss": "https://identity.example.test",
            "sub": "user-123",
            "owner": "built-in",
            "iat": now,
            "exp": now + 600,
            "name": "Test User",
            "roles": ["editor", "provider-admin"],
            "scope": "profile:read root:all investment:admin",
        }

    async def end_session_url(
        self, *, post_logout_redirect_uri: str, state: str
    ) -> str | None:
        return "https://identity.example.test/api/logout?" + urlencode(
            {"post_logout_redirect_uri": post_logout_redirect_uri, "state": state}
        )


class UnreachableLogoutOidc(FakeOidc):
    async def end_session_url(self, **_: str) -> str | None:
        raise ServiceUnavailableError("OIDC discovery unavailable")


class NoUserDirectory:
    async def upsert(self, **_: object) -> dict[str, object]:
        raise AssertionError("the user directory must not be reached in this test")


def build(cls: type[AuthService], surface: str, config: Settings) -> AuthService:
    return cls(
        surface,  # type: ignore[arg-type]
        config,
        FakeOidc(),
        sessions=RedisSessionStore(),
        users=NoUserDirectory(),
    )


class StubAuthService(AuthService):
    async def _load_or_create_user(
        self, issuer: str, subject: str, claims: dict[str, object]
    ) -> dict[str, object]:
        del issuer, subject, claims
        return {
            "id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
            "display_name": "Test User",
            "email": "user@example.test",
            "roles": ["editor"],
            "scopes": ["profile:read", "knowbench:admin"],
        }


def settings() -> Settings:
    return Settings(
        _env_file=None,
        admin_casdoor_client_id="tpl-admin-client",
        admin_casdoor_client_secret="admin-secret",
        admin_casdoor_redirect_uri="https://admin.example.test/api/auth/admin/callback",
        admin_frontend_base_url="https://admin.example.test",
        admin_auth_role_allowlist="editor,member",
        admin_auth_scope_allowlist="knowbench:admin,profile:read",
        web_casdoor_client_id="tpl-web-client",
        web_casdoor_client_secret="web-secret",
        web_casdoor_redirect_uri="https://web.example.test/api/auth/web/callback",
        web_frontend_base_url="https://web.example.test",
        web_auth_role_allowlist="editor,member",
        web_auth_scope_allowlist="profile:read",
    )


@pytest.mark.asyncio
async def test_surface_transaction_and_session_namespaces_cannot_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    config = settings()
    admin = build(StubAuthService, "admin", config)
    web = build(StubAuthService, "web", config)

    start = await admin.begin_login("/settings")
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    with pytest.raises(UnauthorizedError, match="transaction is invalid"):
        await web.complete_login(
            code="code", state=state, transaction_id=start.transaction_id
        )

    session_start, return_to = await admin.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    assert return_to == "/settings"
    session = await admin.get_browser_session(session_start.session_id)
    assert session is not None and session.principal.surface == "admin"
    assert await web.get_browser_session(session_start.session_id) is None
    raw = redis.values[
        f"{config.browser_profile('admin').session_key_prefix}"
        f"{session_start.session_id}"
    ]
    assert "access_token" not in raw and "id_token" not in raw


@pytest.mark.asyncio
async def test_admin_signup_is_forbidden_but_web_signup_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    config = settings()
    with pytest.raises(UnauthorizedError):
        await build(StubAuthService, "admin", config).begin_login(mode="signup")
    result = await build(StubAuthService, "web", config).begin_login(mode="signup")
    assert result.transaction_id


@pytest.mark.asyncio
async def test_csrf_is_bound_to_each_surface_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    service = build(StubAuthService, "web", settings())
    start = await service.begin_login()
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    created, _ = await service.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    session = await service.get_browser_session(created.session_id)
    assert session is not None
    with pytest.raises(ForbiddenError, match="origin"):
        service.validate_csrf(
            session=session,
            method="POST",
            origin="https://admin.example.test",
            csrf_token=session.csrf_token,
        )
    service.validate_csrf(
        session=session,
        method="POST",
        origin="https://web.example.test",
        csrf_token=session.csrf_token,
    )


@pytest.mark.asyncio
async def test_policy_or_surface_change_invalidates_existing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    config = settings()
    profile = config.browser_profile("web")
    now = datetime.now(UTC)
    session = BrowserSession(
        principal=Principal(
            actor_type="user",
            subject="user-123",
            issuer="https://identity.example.test",
            app="knowbench",
            surface="admin",
            audience=profile.client_id,
            actor_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
            authenticated_at=now,
            expires_at=now + timedelta(minutes=5),
            policy_version=profile.policy_version,
        ),
        csrf_token="csrf-token-with-at-least-thirty-two-characters",
    )
    redis.values[f"{profile.session_key_prefix}wrong"] = session.model_dump_json()
    service = build(AuthService, "web", config)
    assert await service.get_browser_session("wrong") is None
    assert not redis.values


def test_provider_claims_are_filtered_by_surface_local_allowlist() -> None:
    service = build(AuthService, "web", settings())
    assert service._allowed_claims(
        (["editor", "provider-admin"],), service.profile.role_allowlist
    ) == ["editor"]
    assert service._allowed_claims(
        ("profile:read root:all",), service.profile.scope_allowlist
    ) == ["profile:read"]


def test_casdoor_role_objects_are_read_by_name() -> None:
    """Casdoor 4.12 的 JWT：roles 是角色对象列表（待办 98 实测的形状）。"""
    service = build(AuthService, "web", settings())
    role = {
        "owner": "built-in",
        "name": "editor",
        "displayName": "editor",
        "createdTime": "2026-10-16T23:40:00+08:00",
        "description": "",
        "users": ["built-in/admin"],
        "groups": [],
        "roles": [],
        "domains": [],
        "isEnabled": True,
    }
    allow = service.profile.role_allowlist
    assert service._allowed_claims(([role],), allow) == ["editor"]
    assert service._allowed_claims(([role | {"isEnabled": False}],), allow) == []
    assert service._allowed_claims(([role | {"name": "provider-admin"}],), allow) == []
    assert service._allowed_claims(([{"owner": "built-in"}, 7, None],), allow) == []


class OwnerOidc(FakeOidc):
    def __init__(self, owner: object) -> None:
        self.owner = owner

    async def exchange_authorization_code(self, **values: str) -> dict[str, object]:
        claims = await super().exchange_authorization_code(**values)
        if self.owner is None:
            claims.pop("owner")
        else:
            claims["owner"] = self.owner
        return claims


async def _login_as(
    surface: str, owner: object, config: Settings, monkeypatch: pytest.MonkeyPatch
) -> object:
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    service = StubAuthService(
        surface,  # type: ignore[arg-type]
        config,
        OwnerOidc(owner),
        sessions=RedisSessionStore(),
        users=NoUserDirectory(),
    )
    start = await service.begin_login()
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    return await service.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )


def user_org_settings() -> Settings:
    return settings().model_copy(
        update={
            "casdoor_organization": "built-in",
            "web_casdoor_organizations": "sunmoonai,partner",
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["sunmoonai", "partner", None, 7, ""])
async def test_admin_surface_only_accepts_the_administrator_organization(
    owner: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ForbiddenError, match="cannot sign in here"):
        await _login_as("admin", owner, user_org_settings(), monkeypatch)


@pytest.mark.asyncio
async def test_admin_surface_accepts_built_in(monkeypatch: pytest.MonkeyPatch) -> None:
    session_start, _ = await _login_as(
        "admin", "built-in", user_org_settings(), monkeypatch
    )  # type: ignore[misc]
    assert session_start.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["sunmoonai", "partner"])
async def test_web_surface_accepts_every_listed_user_organization(
    owner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_start, _ = await _login_as("web", owner, user_org_settings(), monkeypatch)  # type: ignore[misc]
    assert session_start.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["built-in", "other", None])
async def test_web_surface_rejects_organizations_outside_the_list(
    owner: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ForbiddenError, match="cannot sign in here"):
        await _login_as("web", owner, user_org_settings(), monkeypatch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("web_casdoor_organizations", ""),
        ("web_casdoor_organizations", " , "),
        ("web_casdoor_organizations", "sunmoonai,bad name"),
        ("casdoor_organization", ""),
        ("casdoor_organization", "built-in,sunmoonai"),
    ],
)
def test_browser_identity_requires_valid_organizations(field: str, value: str) -> None:
    config = settings().model_copy(
        update={"casdoor_endpoint": "https://identity.example.test", field: value}
    )
    with pytest.raises(ValueError, match="ORGANIZATION"):
        config.require_browser_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "landing"),
    [
        ("web", "https://web.example.test/api/auth/web/logged-out"),
        ("admin", "https://admin.example.test/api/auth/admin/logged-out"),
    ],
)
async def test_end_session_deletes_locally_and_returns_provider_logout(
    surface: str, landing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    config = settings()
    service = build(StubAuthService, surface, config)
    start = await service.begin_login()
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    session_start, _ = await service.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    raw = redis.values[
        f"{config.browser_profile(surface).session_key_prefix}{session_start.session_id}"  # type: ignore[arg-type]
    ]
    assert "id_token" not in raw  # logout does not need a stored token

    url = await service.end_session(session_start.session_id)
    assert url is not None
    query = parse_qs(urlsplit(url).query)
    assert query["post_logout_redirect_uri"] == [landing]
    assert service.post_logout_redirect_uri == landing
    assert len(query["state"][0]) >= 16
    assert await service.get_browser_session(session_start.session_id) is None


@pytest.mark.asyncio
async def test_end_session_falls_back_to_local_logout_when_provider_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    service = StubAuthService(
        "web",  # type: ignore[arg-type]
        settings(),
        UnreachableLogoutOidc(),
        sessions=RedisSessionStore(),
        users=NoUserDirectory(),
    )
    start = await service.begin_login()
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    session_start, _ = await service.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    assert await service.end_session(session_start.session_id) is None
    assert await service.get_browser_session(session_start.session_id) is None


class ExchangingOidc(FakeOidc):
    """会交出访问令牌、能换令牌的提供方（SDD 0042）。"""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.exchanged: list[tuple[str, str]] = []

    async def exchange_authorization_code_tokens(
        self, **values: str
    ) -> tuple[dict[str, object], str | None]:
        return await self.exchange_authorization_code(**values), "user-access-token"

    async def exchange_token(self, *, subject_token: str, audience: str) -> str:
        if self.refuse:
            raise UnauthorizedError("Sign in again", code="reauth_required")
        self.exchanged.append((subject_token, audience))
        return f"for-{audience}"


async def _session_with(oidc: object, monkeypatch: pytest.MonkeyPatch):
    redis = FakeRedis()
    monkeypatch.setattr(
        session_store_module, "get_redis", lambda: FakeRedisHolder(redis)
    )
    config = settings()
    service = StubAuthService(
        "web",
        config,
        oidc,  # type: ignore[arg-type]
        sessions=RedisSessionStore(),
        users=NoUserDirectory(),
    )
    start = await service.begin_login()
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    session_start, _ = await service.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    key = (
        f"{config.browser_profile('web').session_key_prefix}{session_start.session_id}"
    )
    return service, session_start.session_id, redis, key


@pytest.mark.asyncio
async def test_the_access_token_is_kept_beside_the_session_and_traded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oidc = ExchangingOidc()
    service, session_id, redis, key = await _session_with(oidc, monkeypatch)
    # 另存一个键，会话本身的内容不变、不带令牌
    assert redis.values[key + ":access"] == "user-access-token"
    assert "user-access-token" not in redis.values[key]
    token = await service.user_token(session_id, audience="knowbase-client")
    assert token == "for-knowbase-client"
    assert oidc.exchanged == [("user-access-token", "knowbase-client")]
    # 退出时一起删
    await service.end_session(session_id)
    assert key + ":access" not in redis.values


@pytest.mark.asyncio
async def test_trading_needs_a_live_session_and_a_kept_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session_id, redis, key = await _session_with(ExchangingOidc(), monkeypatch)
    with pytest.raises(UnauthorizedError):
        await service.user_token("no-such-session", audience="knowbase")
    # 部署前建的会话没存令牌：要用户重新登录
    del redis.values[key + ":access"]
    with pytest.raises(UnauthorizedError) as caught:
        await service.user_token(session_id, audience="knowbase")
    assert caught.value.code == "reauth_required"


@pytest.mark.asyncio
async def test_a_refused_trade_asks_the_user_to_sign_in_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session_id, *_ = await _session_with(
        ExchangingOidc(refuse=True), monkeypatch
    )
    with pytest.raises(UnauthorizedError) as caught:
        await service.user_token(session_id, audience="knowbase")
    assert caught.value.code == "reauth_required"


@pytest.mark.asyncio
async def test_providers_without_the_capability_still_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session_id, redis, key = await _session_with(FakeOidc(), monkeypatch)
    assert key in redis.values and key + ":access" not in redis.values
    with pytest.raises(ServiceUnavailableError) as caught:
        await service.user_token(session_id, audience="knowbase")
    assert caught.value.code == "token_exchange_unavailable"
