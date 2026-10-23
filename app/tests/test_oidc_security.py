from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.application.errors.exceptions import (
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings


def settings() -> Settings:
    return Settings(
        _env_file=None,
        env="production",
        casdoor_endpoint="https://identity.example.test",
        admin_casdoor_client_id="tpl-admin-client",
        admin_casdoor_client_secret="admin-secret",
        admin_casdoor_redirect_uri="https://admin.example.test/api/auth/admin/callback",
        admin_frontend_base_url="https://admin.example.test",
        admin_frontend_allowed_origins="https://admin.example.test",
        web_casdoor_client_id="tpl-web-client",
        web_casdoor_client_secret="web-secret",
        web_casdoor_redirect_uri="https://web.example.test/api/auth/web/callback",
        web_frontend_base_url="https://web.example.test",
        web_frontend_allowed_origins="https://web.example.test",
        allowed_hosts="admin.example.test,web.example.test",
    )


def token(key: RSAKey, audience: str, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://identity.example.test",
        "sub": "user-123",
        "aud": audience,
        "iat": now,
        "exp": now + 300,
        "nonce": "nonce-123",
    }
    claims.update(overrides)
    return jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        claims,
        key,
        algorithms=["RS256"],
    )


def client(
    surface: str,
    encoded: str,
    key: RSAKey,
    discovery_extra: dict[str, str] | None = None,
) -> OidcProviderClient:
    config = settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": (
                        "https://identity.example.test/login/oauth/authorize"
                    ),
                    "token_endpoint": (
                        "https://identity.example.test/api/login/oauth/access_token"
                    ),
                    "jwks_uri": "https://identity.example.test/.well-known/jwks",
                    **(discovery_extra or {}),
                },
            )
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.url.path.endswith("/access_token"):
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["verifier-123"]
            return httpx.Response(200, json={"id_token": encoded})
        raise AssertionError(f"unexpected request: {request.url}")

    return OidcProviderClient(
        config,
        config.browser_profile(surface),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "audience"),
    [("admin", "tpl-admin-client"), ("web", "tpl-web-client")],
)
async def test_each_surface_verifies_its_own_audience(
    surface: str, audience: str
) -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    oidc = client(surface, token(key, audience), key)
    claims = await oidc.exchange_authorization_code(
        code="code", code_verifier="verifier-123", nonce="nonce-123"
    )
    assert claims["aud"] == audience


@pytest.mark.asyncio
async def test_admin_token_cannot_be_replayed_at_web_client() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    with pytest.raises(UnauthorizedError) as error:
        oidc = client("web", token(key, "tpl-admin-client"), key)
        await oidc.exchange_authorization_code(
            code="code", code_verifier="verifier-123", nonce="nonce-123"
        )
    assert error.value.code == "audience_mismatch"


@pytest.mark.asyncio
async def test_signup_is_web_only_and_uses_provider_signup_endpoint() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    web_client = client("web", token(key, "tpl-web-client"), key)
    url = await web_client.build_authorization_url(
        state="state", nonce="nonce-123", code_challenge="challenge", mode="signup"
    )
    assert urlsplit(url).path == "/signup/oauth/authorize"
    with pytest.raises(UnauthorizedError):
        admin_client = client("admin", token(key, "tpl-admin-client"), key)
        await admin_client.build_authorization_url(
            state="state", nonce="nonce-123", code_challenge="challenge", mode="signup"
        )


@pytest.mark.asyncio
async def test_discovery_cannot_redirect_jwks_cross_origin() -> None:
    config = settings()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": "https://identity.example.test",
                "authorization_endpoint": "https://identity.example.test/authorize",
                "token_endpoint": "https://identity.example.test/token",
                "jwks_uri": "https://attacker.example.test/jwks",
            },
        )

    oidc = OidcProviderClient(
        config,
        config.browser_profile("web"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ServiceUnavailableError, match="cross-origin"):
        await oidc.get_metadata()


@pytest.mark.asyncio
async def test_client_credentials_use_explicit_service_identity() -> None:
    config = settings()
    observed: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": "https://identity.example.test/authorize",
                    "token_endpoint": "https://identity.example.test/token",
                    "jwks_uri": "https://identity.example.test/jwks",
                },
            )
        observed.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "opaque", "expires_in": 60})

    oidc = OidcProviderClient(
        config,
        config.browser_profile("web"),
        transport=httpx.MockTransport(handler),
    )
    await oidc.exchange_client_credentials(
        scope="knowbase:read",
        client_id="service-client",
        client_secret="service-secret",
    )
    assert observed["client_id"] == ["service-client"]
    assert observed["scope"] == ["knowbase:read"]


def exchanging(status: int, body: object, observed: dict[str, list[str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": "https://identity.example.test/authorize",
                    "token_endpoint": "https://identity.example.test/token",
                    "jwks_uri": "https://identity.example.test/jwks",
                },
            )
        observed.update(parse_qs(request.content.decode()))
        return httpx.Response(status, json=body)

    config = settings()
    return OidcProviderClient(
        config, config.browser_profile("web"), transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_token_exchange_trades_the_users_token_as_this_client() -> None:
    """RFC 8693（SDD 0042）：用本面的客户端身份，把用户的访问令牌换成给 knowbase 的。"""
    observed: dict[str, list[str]] = {}
    oidc = exchanging(200, {"access_token": "for-knowbase"}, observed)
    assert (
        await oidc.exchange_token(subject_token="user-token", audience="knowbase")
        == "for-knowbase"
    )
    assert observed["grant_type"] == ["urn:ietf:params:oauth:grant-type:token-exchange"]
    assert observed["subject_token"] == ["user-token"]
    assert observed["subject_token_type"] == [
        "urn:ietf:params:oauth:token-type:access_token"
    ]
    assert observed["audience"] == ["knowbase"]
    assert observed["client_id"] == [settings().browser_profile("web").client_id]


@pytest.mark.asyncio
async def test_a_refused_token_exchange_asks_to_sign_in_again() -> None:
    oidc = exchanging(400, {"error": "invalid_grant"}, {})
    with pytest.raises(UnauthorizedError) as caught:
        await oidc.exchange_token(subject_token="revoked", audience="knowbase")
    assert caught.value.code == "reauth_required"
    broken = exchanging(500, {}, {})
    with pytest.raises(ServiceUnavailableError):
        await broken.exchange_token(subject_token="t", audience="knowbase")


@pytest.mark.asyncio
async def test_workload_access_token_requires_exact_audience_and_signature() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    oidc = client("admin", token(key, "knowbase-internal"), key)
    claims = await oidc.verify_access_token(
        token(key, "knowbase-internal", scope="knowbase:retrieve"),
        audience="knowbase-internal",
    )
    assert claims["sub"] == "user-123"
    with pytest.raises(UnauthorizedError) as error:
        await oidc.verify_access_token(
            token(key, "wrong-audience"), audience="knowbase-internal"
        )
    assert error.value.code == "audience_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "client_id"),
    [("admin", "tpl-admin-client"), ("web", "tpl-web-client")],
)
async def test_logout_url_names_this_client_and_landing(
    surface: str, client_id: str
) -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    oidc = client(
        surface,
        token(key, client_id),
        key,
        {"end_session_endpoint": "https://identity.example.test/api/logout"},
    )
    url = await oidc.end_session_url(
        post_logout_redirect_uri="https://web.example.test/api/auth/web/logged-out",
        state="state-123",
    )
    assert url is not None
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://identity.example.test/api/logout"
    )
    assert parse_qs(parts.query) == {
        "client_id": [client_id],
        "post_logout_redirect_uri": [
            "https://web.example.test/api/auth/web/logged-out"
        ],
        "state": ["state-123"],
    }


@pytest.mark.asyncio
async def test_logout_url_is_absent_without_provider_logout() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    oidc = client("web", token(key, "tpl-web-client"), key)
    assert (
        await oidc.end_session_url(
            post_logout_redirect_uri="https://web.example.test/api/auth/web/logged-out",
            state="state-123",
        )
        is None
    )


@pytest.mark.asyncio
async def test_discovery_cannot_redirect_logout_cross_origin() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    oidc = client(
        "web",
        token(key, "tpl-web-client"),
        key,
        {"end_session_endpoint": "https://evil.example.test/api/logout"},
    )
    with pytest.raises(ServiceUnavailableError, match="end_session_endpoint"):
        await oidc.end_session_url(
            post_logout_redirect_uri="https://web.example.test/api/auth/web/logged-out",
            state="state-123",
        )
