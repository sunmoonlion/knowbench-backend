"""Reviewed service configuration and connection-management requests."""

from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from app.domain.workbench.capabilities import Strict, digest

Scope = Annotated[str, Field(pattern=r"^[\x21\x23-\x5B\x5D-\x7E]{1,128}$")]


class OAuthProvider(Strict):
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    protocol: Literal["mcp", "a2a"]
    service_url: str
    issuer: str
    authorization_url: str
    token_url: str
    account_url: str
    revocation_url: str | None = None
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: SecretStr | None = None
    scopes: tuple[Scope, ...] = Field(min_length=1, max_length=64)
    tool_scopes: dict[str, tuple[Scope, ...]] = Field(min_length=1, max_length=128)
    account_field: str = Field(default="sub", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    label_field: str = Field(default="name", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    # MCP requires a resource indicator; other service profiles can opt in.
    resource_indicator: bool = True

    @model_validator(mode="after")
    def reviewed(self):
        for endpoint in (
            self.service_url,
            self.issuer,
            self.authorization_url,
            self.token_url,
            self.account_url,
            self.revocation_url,
        ):
            if endpoint is None:
                continue
            parts = urlsplit(endpoint)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.port not in (None, 443)
                or parts.query
                or parts.fragment
                or any(c.isspace() for c in endpoint)
                or "\\" in endpoint
            ):
                raise ValueError("OAuth endpoints require fixed HTTPS URLs")
        if urlsplit(self.account_url).netloc != urlsplit(self.service_url).netloc:
            raise ValueError(
                "The account endpoint must belong to the reviewed resource origin"
            )
        if self.protocol == "mcp" and not self.resource_indicator:
            raise ValueError("MCP requires a resource indicator")
        if any(
            not set(scopes) <= set(self.scopes) for scopes in self.tool_scopes.values()
        ):
            raise ValueError("Tool scopes exceed the provider scope")
        if self.protocol == "a2a" and set(self.tool_scopes) != {"a2a_get_task"}:
            raise ValueError("The A2A connection profile supports GetTask")
        return self

    def binding(self):
        return digest(self.model_dump(mode="json", exclude={"client_secret"}))


class OAuthStart(Strict):
    provider_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    locale: Literal["zh-CN", "en"] = "zh-CN"
    return_to: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^/(zh-CN|en)/workbench/(chat/[a-zA-Z0-9-]+|projects/[a-zA-Z0-9-]+/c/[a-zA-Z0-9-]+)$",
    )
    connection_id: UUID | None = None
    revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def target(self):
        if (self.connection_id is None) != (self.revision is None):
            raise ValueError("Reauthorization requires an exact connection revision")
        return self
