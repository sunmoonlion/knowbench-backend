"""What browser authentication needs from the outside world.

Implementations live in `app/infrastructure/`; `app/bootstrap/auth.py` wires them.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class IdentityProvider(Protocol):
    """The OIDC provider, already bound to one browser surface."""

    async def build_authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        mode: str = "login",
    ) -> str: ...

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> dict[str, Any]:
        """Return the claims of the verified identity token."""
        ...

    async def end_session_url(
        self, *, post_logout_redirect_uri: str, state: str
    ) -> str | None:
        """The provider logout URL that also ends its own session; None if it has none.

        Identifies this client by client_id, so no token has to be kept for logout.
        """
        ...


@runtime_checkable
class TokenExchange(Protocol):
    """Optional provider capability: keep and trade the user's access token (SDD 0042).

    Apps that call knowbase on the user's behalf exchange the access token the provider
    issued to this client for one addressed to knowbase (RFC 8693). Providers without
    the capability still sign users in; only the exchange is unavailable.
    """

    async def exchange_authorization_code_tokens(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Verified identity-token claims and the access token (None if absent)."""
        ...

    async def exchange_token(self, *, subject_token: str, audience: str) -> str:
        """An access token for `audience` that still names the same user."""
        ...


class SessionStore(Protocol):
    """Expiring browser state: login transactions and sessions."""

    async def create(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        """Store only if the key is absent. False means it already existed."""
        ...

    async def read(self, key: str) -> str | None: ...

    async def take(self, key: str) -> str | None:
        """Read and delete in one step, so a value can be used once."""
        ...

    async def delete(self, key: str) -> None: ...


class UserDirectory(Protocol):
    """Local users keyed by the provider subject; the issuer is recorded, not a key."""

    async def upsert(
        self,
        *,
        issuer: str,
        subject: str,
        username: str,
        email: str | None,
        display_name: str | None,
        roles: list[str],
        scopes: list[str],
    ) -> dict[str, Any]:
        """Create or refresh; return id, email, display_name, roles, scopes."""
        ...
