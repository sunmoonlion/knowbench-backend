from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError as PydanticValidationError

from app.application.errors.exceptions import (
    AppException,
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.application.ports.auth import (
    IdentityProvider,
    SessionStore,
    TokenExchange,
    UserDirectory,
)
from app.domain.security import BrowserSession, LoginStart, Principal, SessionStart
from core.config import BrowserSurface, BrowserSurfaceProfile, Settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 用户的访问令牌另存一个键，和会话同寿命；只在服务端，换令牌时用（SDD 0042）
ACCESS_TOKEN_SUFFIX = ":access"


class AuthService:
    """Shared authentication kernel parameterized by an immutable surface profile."""

    def __init__(
        self,
        surface: BrowserSurface,
        settings: Settings,
        identity_provider: IdentityProvider,
        *,
        sessions: SessionStore,
        users: UserDirectory,
    ) -> None:
        self._settings = settings
        self._profile = self._settings.browser_profile(surface)
        self._oidc = identity_provider
        self._sessions = sessions
        self._users = users

    @property
    def surface(self) -> BrowserSurface:
        return self._profile.surface

    @property
    def profile(self) -> BrowserSurfaceProfile:
        return self._profile

    def _normalize_return_to(self, value: str | None) -> str:
        fallback = self._profile.default_return_to
        if not value:
            return fallback
        try:
            normalized = self._settings._validate_relative_path(
                value, field="return_to"
            )
        except ValueError:
            return fallback
        if not any(
            self._settings._path_is_allowed(normalized, allowed)
            for allowed in self._profile.allowed_return_paths
        ):
            return fallback
        return normalized

    @staticmethod
    def _new_secret(bytes_count: int = 32) -> str:
        return secrets.token_urlsafe(bytes_count)

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    async def begin_login(
        self,
        return_to: str | None = None,
        mode: str = "login",
    ) -> LoginStart:
        if mode not in {"login", "signup"} or (
            mode == "signup" and self.surface != "web"
        ):
            raise UnauthorizedError(
                "The login transaction is invalid",
                code="oidc_transaction_invalid",
            )
        transaction_id = self._new_secret()
        state = self._new_secret()
        nonce = self._new_secret()
        code_verifier = self._new_secret(64)
        transaction = {
            "surface": self.surface,
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "return_to": self._normalize_return_to(return_to),
            "created_at": int(time.time()),
        }
        key = f"{self._profile.transaction_key_prefix}{transaction_id}"
        stored = await self._sessions.create(
            key,
            json.dumps(transaction, separators=(",", ":")),
            ttl_seconds=self._settings.auth_transaction_ttl_seconds,
        )
        if not stored:
            raise UnauthorizedError(
                "The login transaction is invalid",
                code="oidc_transaction_invalid",
            )
        try:
            authorization_url = await self._oidc.build_authorization_url(
                state=state,
                nonce=nonce,
                code_challenge=self._pkce_challenge(code_verifier),
                mode=mode,
            )
        except Exception:
            await self._sessions.delete(key)
            raise
        return LoginStart(
            authorization_url=authorization_url,
            transaction_id=transaction_id,
        )

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        transaction_id: str | None,
    ) -> tuple[SessionStart, str]:
        if not transaction_id or len(transaction_id) > 256:
            raise self._transaction_error()
        key = f"{self._profile.transaction_key_prefix}{transaction_id}"
        raw = await self._sessions.take(key)
        if not raw:
            raise self._transaction_error()
        try:
            transaction = json.loads(raw)
            transaction_surface = transaction["surface"]
            expected_state = transaction["state"]
            nonce = transaction["nonce"]
            code_verifier = transaction["code_verifier"]
            return_to = self._normalize_return_to(transaction.get("return_to"))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise self._transaction_error() from exc
        if transaction_surface != self.surface:
            raise self._transaction_error()
        if not isinstance(expected_state, str) or not hmac.compare_digest(
            expected_state, state
        ):
            raise self._transaction_error()
        if not isinstance(nonce, str) or not isinstance(code_verifier, str):
            raise self._transaction_error()

        access_token: str | None = None
        if isinstance(self._oidc, TokenExchange):
            claims, access_token = await self._oidc.exchange_authorization_code_tokens(
                code=code,
                code_verifier=code_verifier,
                nonce=nonce,
            )
        else:
            claims = await self._oidc.exchange_authorization_code(
                code=code,
                code_verifier=code_verifier,
                nonce=nonce,
            )
        principal = await self._principal_from_verified_claims(claims)
        session_id = self._new_secret()
        browser_session = BrowserSession(
            principal=principal,
            csrf_token=self._new_secret(),
        )
        ttl = max(1, int((principal.expires_at - datetime.now(UTC)).total_seconds()))
        stored = await self._sessions.create(
            f"{self._profile.session_key_prefix}{session_id}",
            browser_session.model_dump_json(),
            ttl_seconds=ttl,
        )
        if not stored:
            raise UnauthorizedError(
                "The session could not be created",
                code="session_expired",
            )
        if access_token:
            await self._sessions.create(
                f"{self._profile.session_key_prefix}{session_id}{ACCESS_TOKEN_SUFFIX}",
                access_token,
                ttl_seconds=ttl,
            )
        return (
            SessionStart(session_id=session_id, expires_at=principal.expires_at),
            return_to,
        )

    @staticmethod
    def _transaction_error() -> UnauthorizedError:
        return UnauthorizedError(
            "The login transaction is invalid",
            code="oidc_transaction_invalid",
        )

    async def _principal_from_verified_claims(
        self, claims: dict[str, Any]
    ) -> Principal:
        issuer = str(claims["iss"])
        subject = str(claims["sub"])
        provider_exp = int(claims["exp"])
        issued_at = int(claims["iat"])
        now = int(time.time())
        expires_at_epoch = min(provider_exp, now + self._settings.session_ttl_seconds)
        if expires_at_epoch <= now:
            raise UnauthorizedError(
                "The identity token is invalid",
                code="token_invalid",
            )
        # Casdoor 的 JWT 带用户所属组织（owner）。管理端只收管理员组织，网页端只收
        # 配置的用户组织；哪怕 Casdoor 里应用的组织被改错，普通用户也进不了管理端。
        organization = claims.get("owner")
        if not isinstance(
            organization, str
        ) or organization not in self._settings.allowed_organizations(self.surface):
            raise ForbiddenError(
                "This account cannot sign in here", code="organization_not_allowed"
            )
        local_user = await self._load_or_create_user(issuer, subject, claims)
        return Principal(
            actor_type="user",
            subject=subject,
            issuer=issuer,
            app=self._settings.app_slug,
            surface=self.surface,
            audience=self._profile.client_id,
            actor_id=local_user["id"],
            display_name=local_user.get("display_name"),
            email=local_user.get("email"),
            roles=tuple(local_user.get("roles") or ()),
            scopes=frozenset(local_user.get("scopes") or ()),
            authenticated_at=datetime.fromtimestamp(issued_at, tz=UTC),
            expires_at=datetime.fromtimestamp(expires_at_epoch, tz=UTC),
            policy_version=self._profile.policy_version,
        )

    async def _load_or_create_user(
        self,
        issuer: str,
        subject: str,
        claims: dict[str, Any],
    ) -> dict[str, Any]:
        username = str(
            claims.get("preferred_username")
            or claims.get("name")
            or claims.get("email")
            or subject
        ).strip()[:255]
        email = str(claims.get("email") or "").strip()[:320] or None
        display_name = str(claims.get("name") or username).strip()[:256] or None
        roles = self._allowed_claims(
            (claims.get("roles"), claims.get("role")),
            self._profile.role_allowlist,
        )
        scopes = self._allowed_claims(
            (claims.get("scope"), claims.get("scp"), claims.get("permissions")),
            self._profile.scope_allowlist,
        )
        return await self._users.upsert(
            issuer=issuer,
            subject=subject,
            username=username,
            email=email,
            display_name=display_name,
            roles=roles,
            scopes=scopes,
        )

    @staticmethod
    def _allowed_claims(
        values: tuple[object, ...],
        allowlist: frozenset[str],
    ) -> list[str]:
        selected: set[str] = set()
        for value in values:
            candidates: list[Any]
            if isinstance(value, (list, tuple, set, frozenset)):
                candidates = list(value)
            elif isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    continue
                try:
                    decoded = json.loads(normalized)
                except json.JSONDecodeError:
                    decoded = None
                candidates = (
                    decoded
                    if isinstance(decoded, list)
                    else re.split(r"[,\s]+", normalized)
                )
            else:
                continue
            for candidate in candidates:
                # Casdoor 的 JWT 把角色给成对象列表（name、owner、isEnabled…）：
                # 取 name，停用的不算
                if isinstance(candidate, dict):
                    if candidate.get("isEnabled") is False:
                        continue
                    candidate = candidate.get("name")
                    if not isinstance(candidate, str):
                        continue
                item = str(candidate).strip()
                if item in allowlist:
                    selected.add(item)
        return sorted(selected)

    async def get_browser_session(
        self, session_id: str | None
    ) -> BrowserSession | None:
        if not session_id or len(session_id) > 256:
            return None
        key = f"{self._profile.session_key_prefix}{session_id}"
        raw = await self._sessions.read(key)
        if not raw:
            return None
        try:
            session = BrowserSession.model_validate_json(raw)
        except PydanticValidationError:
            await self._sessions.delete(key)
            return None
        principal = session.principal
        if principal.expires_at <= datetime.now(UTC):
            await self._sessions.delete(key)
            return None
        if (
            principal.app != self._settings.app_slug
            or principal.surface != self.surface
            or principal.audience != self._profile.client_id
            or principal.policy_version != self._profile.policy_version
        ):
            await self._sessions.delete(key)
            return None
        return session

    async def delete_session(self, session_id: str | None) -> None:
        if session_id and len(session_id) <= 256:
            key = f"{self._profile.session_key_prefix}{session_id}"
            await self._sessions.delete(key)
            await self._sessions.delete(key + ACCESS_TOKEN_SUFFIX)

    async def user_token(self, session_id: str | None, *, audience: str) -> str:
        """A token for `audience` on behalf of this session's user (SDD 0042).

        Sessions created before the access token was kept, or whose token the provider
        no longer accepts, get `reauth_required`: the user signs in again.
        """
        if await self.get_browser_session(session_id) is None:
            raise UnauthorizedError()
        if not isinstance(self._oidc, TokenExchange):
            raise ServiceUnavailableError(
                "Token exchange is not available", code="token_exchange_unavailable"
            )
        subject = await self._sessions.read(
            f"{self._profile.session_key_prefix}{session_id}{ACCESS_TOKEN_SUFFIX}"
        )
        if not subject:
            raise UnauthorizedError("Sign in again to continue", code="reauth_required")
        return await self._oidc.exchange_token(subject_token=subject, audience=audience)

    @property
    def post_logout_redirect_uri(self) -> str:
        """Where the provider sends the browser after logout.

        /api/auth/<surface>/logged-out on the origin of the validated callback URI;
        registered with the provider next to the callback.
        """
        callback = urlsplit(self._profile.redirect_uri)
        path = f"/api/auth/{self.surface}/logged-out"
        return urlunsplit((callback.scheme, callback.netloc, path, "", ""))

    async def end_session(self, session_id: str | None) -> str | None:
        """Delete the local session; return the provider logout URL that ends SSO too.

        None means local logout only (the provider has no logout endpoint or cannot be
        reached); local logout never depends on the provider.
        """
        await self.delete_session(session_id)
        try:
            return await self._oidc.end_session_url(
                post_logout_redirect_uri=self.post_logout_redirect_uri,
                state=self._new_secret(16),
            )
        except AppException:
            return None

    async def cancel_login(self, transaction_id: str | None) -> None:
        if transaction_id and len(transaction_id) <= 256:
            await self._sessions.delete(
                f"{self._profile.transaction_key_prefix}{transaction_id}"
            )

    def validate_csrf(
        self,
        *,
        session: BrowserSession,
        method: str,
        origin: str | None,
        csrf_token: str | None,
    ) -> None:
        if method.upper() in SAFE_METHODS:
            return
        if not origin or origin not in self._profile.frontend_origins:
            raise ForbiddenError("Request origin denied")
        if not csrf_token or not hmac.compare_digest(session.csrf_token, csrf_token):
            raise ForbiddenError("CSRF validation failed")

    @staticmethod
    def require_scopes(
        principal: Principal, required: set[str] | frozenset[str]
    ) -> None:
        if not principal.has_scopes(required):
            raise ForbiddenError("Required scope missing")
