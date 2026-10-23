from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from app.application.errors.exceptions import (
    ServiceUnavailableError,
    UnauthorizedError,
)
from core.config import BrowserSurfaceProfile, Settings


@dataclass(frozen=True)
class OidcMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None


class OidcProviderClient:
    """Strict OIDC client with optional host-preserving in-cluster transport."""

    def __init__(
        self,
        settings: Settings,
        profile: BrowserSurfaceProfile,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._profile = profile
        self._transport = transport
        self._metadata: OidcMetadata | None = None
        self._metadata_loaded_at = 0.0
        self._key_set: KeySet | None = None
        self._keys_loaded_at = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._settings.casdoor_verify_ssl,
            timeout=self._settings.auth_http_timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        )

    def _validate_provider_url(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ServiceUnavailableError(f"OIDC metadata missing {field}")
        parsed = urlsplit(value)
        endpoint = urlsplit(self._settings.casdoor_endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ServiceUnavailableError(f"OIDC metadata invalid {field}")
        if parsed.username or parsed.password or parsed.fragment:
            raise ServiceUnavailableError(f"OIDC metadata invalid {field}")
        if self._settings.is_production and parsed.scheme != "https":
            raise ServiceUnavailableError("OIDC metadata requires HTTPS")
        if (
            not endpoint.hostname
            or parsed.hostname.lower() != endpoint.hostname.lower()
        ):
            raise ServiceUnavailableError(f"OIDC metadata cross-origin {field}")
        if parsed.port != endpoint.port:
            raise ServiceUnavailableError(f"OIDC metadata cross-origin {field}")
        return value

    def _backchannel_target(
        self, public_url: str, field: str
    ) -> tuple[str, dict[str, str]]:
        public_url = self._validate_provider_url(public_url, field)
        configured = self._settings.casdoor_backchannel_endpoint
        if not configured:
            return public_url, {}
        backchannel = urlsplit(configured)
        if (
            backchannel.scheme not in {"http", "https"}
            or not backchannel.hostname
            or backchannel.username
            or backchannel.password
            or backchannel.path not in {"", "/"}
            or backchannel.query
            or backchannel.fragment
        ):
            raise ServiceUnavailableError("OIDC backchannel endpoint invalid")
        public = urlsplit(public_url)
        endpoint = urlsplit(self._settings.casdoor_endpoint)
        return (
            urlunsplit(
                (
                    backchannel.scheme,
                    backchannel.netloc,
                    public.path,
                    public.query,
                    "",
                )
            ),
            {"Host": endpoint.netloc},
        )

    async def get_metadata(self, *, force_refresh: bool = False) -> OidcMetadata:
        now = time.monotonic()
        if (
            not force_refresh
            and self._metadata is not None
            and now - self._metadata_loaded_at
            < self._settings.auth_discovery_cache_seconds
        ):
            return self._metadata
        discovery_url = self._settings.casdoor_discovery_endpoint
        if not discovery_url:
            raise ServiceUnavailableError("OIDC discovery is not configured")
        discovery_url, routing_headers = self._backchannel_target(
            discovery_url, "discovery"
        )
        try:
            async with self._client() as client:
                response = await client.get(
                    discovery_url,
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC discovery unavailable") from exc
        if response.status_code != 200:
            raise ServiceUnavailableError("OIDC discovery unavailable")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ServiceUnavailableError(
                "OIDC discovery returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ServiceUnavailableError("OIDC discovery returned invalid document")
        metadata = OidcMetadata(
            issuer=self._validate_provider_url(payload.get("issuer"), "issuer"),
            authorization_endpoint=self._validate_provider_url(
                payload.get("authorization_endpoint"), "authorization_endpoint"
            ),
            token_endpoint=self._validate_provider_url(
                payload.get("token_endpoint"), "token_endpoint"
            ),
            jwks_uri=self._validate_provider_url(payload.get("jwks_uri"), "jwks_uri"),
            # Optional (RP-initiated logout); if present, same-origin like the rest.
            end_session_endpoint=(
                self._validate_provider_url(
                    payload["end_session_endpoint"], "end_session_endpoint"
                )
                if payload.get("end_session_endpoint") is not None
                else None
            ),
        )
        self._metadata = metadata
        self._metadata_loaded_at = now
        return metadata

    async def _get_key_set(
        self, metadata: OidcMetadata, *, force_refresh: bool = False
    ) -> KeySet:
        now = time.monotonic()
        if (
            not force_refresh
            and self._key_set is not None
            and now - self._keys_loaded_at < self._settings.auth_jwks_cache_seconds
        ):
            return self._key_set
        try:
            jwks_url, routing_headers = self._backchannel_target(
                metadata.jwks_uri, "jwks_uri"
            )
            async with self._client() as client:
                response = await client.get(
                    jwks_url,
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC JWKS unavailable") from exc
        if response.status_code != 200:
            raise ServiceUnavailableError("OIDC JWKS unavailable")
        try:
            payload = response.json()
            key_set = KeySet.import_key_set(payload)
        except (ValueError, TypeError, JoseError) as exc:
            raise ServiceUnavailableError("OIDC JWKS invalid") from exc
        self._key_set = key_set
        self._keys_loaded_at = now
        return key_set

    async def build_authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        mode: str = "login",
    ) -> str:
        metadata = await self.get_metadata()
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self._profile.client_id,
                "redirect_uri": self._profile.redirect_uri,
                "scope": " ".join(
                    ("openid", "profile", "email", *self._profile.required_scopes)
                ),
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        endpoint = metadata.authorization_endpoint
        if mode == "signup":
            if self._profile.surface != "web":
                raise UnauthorizedError(
                    "The login transaction is invalid",
                    code="oidc_transaction_invalid",
                )
            endpoint = self._validate_provider_url(
                f"{self._settings.casdoor_endpoint.rstrip('/')}/signup/oauth/authorize",
                "signup_endpoint",
            )
        return f"{endpoint}?{params}"

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> dict[str, Any]:
        claims, _ = await self.exchange_authorization_code_tokens(
            code=code, code_verifier=code_verifier, nonce=nonce
        )
        return claims

    async def exchange_authorization_code_tokens(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Claims of the verified identity token, plus the access token if issued."""
        metadata = await self.get_metadata()
        try:
            token_url, routing_headers = self._backchannel_target(
                metadata.token_endpoint, "token_endpoint"
            )
            async with self._client() as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self._profile.client_id,
                        "client_secret": self._profile.client_secret,
                        "code": code,
                        "redirect_uri": self._profile.redirect_uri,
                        "code_verifier": code_verifier,
                    },
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC token endpoint unavailable") from exc
        if response.status_code != 200:
            raise UnauthorizedError(
                "The identity token is invalid", code="token_invalid"
            )
        try:
            token_response = response.json()
        except ValueError as exc:
            raise UnauthorizedError(
                "The identity token is invalid", code="token_invalid"
            ) from exc
        if not isinstance(token_response, dict):
            raise UnauthorizedError(
                "The identity token is invalid", code="token_invalid"
            )
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise UnauthorizedError(
                "The identity token is invalid", code="token_invalid"
            )
        claims = await self.verify_id_token(id_token, nonce=nonce, metadata=metadata)
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            access_token = None
        return claims, access_token

    async def exchange_token(self, *, subject_token: str, audience: str) -> str:
        """RFC 8693: trade the user's access token for one addressed to `audience`.

        Casdoor 4.12 only accepts a token this client was issued itself, keeps the user
        (`sub`), names this client as the acting party (`azp`) and refuses revoked
        tokens; tried on a scratch Casdoor 2026-10-23 (k8s scripts/results, 0042).
        """
        metadata = await self.get_metadata()
        try:
            token_url, routing_headers = self._backchannel_target(
                metadata.token_endpoint, "token_endpoint"
            )
            async with self._client() as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "client_id": self._profile.client_id,
                        "client_secret": self._profile.client_secret,
                        "subject_token": subject_token,
                        "subject_token_type": (
                            "urn:ietf:params:oauth:token-type:access_token"
                        ),
                        "audience": audience,
                    },
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC token endpoint unavailable") from exc
        if response.status_code in (400, 401):
            # 用户令牌过期、被吊销：要用户重新登录，不当成平台故障
            raise UnauthorizedError("Sign in again to continue", code="reauth_required")
        if response.status_code != 200:
            raise ServiceUnavailableError("OIDC token exchange unavailable")
        try:
            token_response = response.json()
        except ValueError as exc:
            raise ServiceUnavailableError("OIDC token exchange unavailable") from exc
        token = (
            token_response.get("access_token")
            if isinstance(token_response, dict)
            else None
        )
        if not isinstance(token, str) or not token:
            raise ServiceUnavailableError("OIDC token exchange unavailable")
        return token

    async def end_session_url(
        self, *, post_logout_redirect_uri: str, state: str
    ) -> str | None:
        """Public logout URL for the browser.

        The provider ends its session and redirects back only to a URI registered for
        this client_id; anything else stops at the provider (Casdoor 4.12, SDD 0027).
        """
        metadata = await self.get_metadata()
        if metadata.end_session_endpoint is None:
            return None
        params = urlencode(
            {
                "client_id": self._profile.client_id,
                "post_logout_redirect_uri": post_logout_redirect_uri,
                "state": state,
            }
        )
        return f"{metadata.end_session_endpoint}?{params}"

    async def exchange_client_credentials(
        self,
        *,
        scope: str,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> dict[str, Any]:
        """Request a service token using the validated Provider transport."""

        metadata = await self.get_metadata()
        try:
            token_url, routing_headers = self._backchannel_target(
                metadata.token_endpoint, "token_endpoint"
            )
            async with self._client() as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": client_id or self._profile.client_id,
                        "client_secret": client_secret or self._profile.client_secret,
                        "scope": scope,
                    },
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC token endpoint unavailable") from exc
        if response.status_code != 200:
            raise UnauthorizedError(
                "The service identity was rejected", code="token_invalid"
            )
        try:
            token_response = response.json()
        except ValueError as exc:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            ) from exc
        if not isinstance(token_response, dict):
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        return token_response

    async def verify_id_token(
        self,
        encoded: str,
        *,
        nonce: str,
        metadata: OidcMetadata | None = None,
    ) -> dict[str, Any]:
        metadata = metadata or await self.get_metadata()
        claims = await self._decode_signed_token(encoded, metadata=metadata)
        if claims.get("iss") != metadata.issuer:
            raise UnauthorizedError(
                "The identity token issuer is invalid",
                code="issuer_mismatch",
            )
        audience = claims.get("aud")
        if audience != self._profile.client_id and audience != [
            self._profile.client_id
        ]:
            raise UnauthorizedError(
                "The identity token audience is invalid",
                code="audience_mismatch",
            )
        try:
            registry = JWTClaimsRegistry(
                leeway=self._settings.auth_clock_skew_seconds,
                iss={"essential": True, "value": metadata.issuer},
                sub={"essential": True},
                aud={"essential": True},
                exp={"essential": True},
                iat={"essential": True},
                nonce={"essential": True, "value": nonce},
            )
            registry.validate(claims)
        except (JoseError, ValueError, TypeError) as exc:
            raise UnauthorizedError(
                "The identity token is invalid", code="token_invalid"
            ) from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise UnauthorizedError(
                "The identity token is invalid", code="token_invalid"
            )
        return claims

    async def verify_access_token(
        self,
        encoded: str,
        *,
        audience: str,
        metadata: OidcMetadata | None = None,
    ) -> dict[str, Any]:
        """Verify a Provider-signed workload JWT without browser assumptions."""

        metadata = metadata or await self.get_metadata()
        claims = await self._decode_signed_token(encoded, metadata=metadata)
        token_audience = claims.get("aud")
        audiences = (
            {token_audience}
            if isinstance(token_audience, str)
            else set(token_audience)
            if isinstance(token_audience, list)
            and all(isinstance(item, str) for item in token_audience)
            else set()
        )
        if audience not in audiences:
            raise UnauthorizedError(
                "The service identity audience is invalid",
                code="audience_mismatch",
            )
        try:
            registry = JWTClaimsRegistry(
                leeway=self._settings.auth_clock_skew_seconds,
                iss={"essential": True, "value": metadata.issuer},
                sub={"essential": True},
                aud={"essential": True},
                exp={"essential": True},
                iat={"essential": True},
            )
            registry.validate(claims)
        except (JoseError, ValueError, TypeError) as exc:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            ) from exc
        return claims

    async def _decode_signed_token(
        self,
        encoded: str,
        *,
        metadata: OidcMetadata,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for refresh in (False, True):
            try:
                key_set = await self._get_key_set(metadata, force_refresh=refresh)
                token = jwt.decode(
                    encoded,
                    key_set,
                    algorithms=self._settings.auth_allowed_algorithm_list,
                )
                if not isinstance(token.claims, dict):
                    raise TypeError("JWT claims must be an object")
                return token.claims
            except UnauthorizedError:
                raise
            except (JoseError, ValueError, TypeError) as exc:
                last_error = exc
        raise UnauthorizedError(
            "The identity token is invalid", code="token_invalid"
        ) from last_error
