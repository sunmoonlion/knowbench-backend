"""调其他应用内部接口的通用客户端（SDD 0027 P1：common/backend/service-client）。

每个「目标 × 关系」一组环境变量，由部署从本应用后端 config 的 service_clients 生成：
`<目标>_INTERNAL_URL`、`<目标>_<关系>_SERVICE_CLIENT_ID`、`_CLIENT_SECRET`、`_SCOPE`、
`_DISCOVERY_URL`、`_BACKCHANNEL_ENDPOINT`。用 client credentials 换短期令牌、缓存到快过
期，带令牌走集群内 HTTPS（CA 由 SSL_CERT_FILE 指定）。目标不可达或回 5xx 抛
ServiceUnavailableError，回 4xx 抛 BadGatewayError（带目标给的 code），不在这里重试。
"""

from __future__ import annotations

import asyncio
import sys
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.application.errors.exceptions import (
    BadGatewayError,
    ServiceUnavailableError,
)
from app.infrastructure.security import OidcProviderClient
from core.config import Settings, get_settings


@dataclass(frozen=True)
class ServiceRelation:
    target: str
    relation: str
    internal_url: str
    client_id: str
    client_secret: str
    scope: str
    discovery_url: str
    backchannel_endpoint: str


def _key(name: str) -> str:
    return name.upper().replace("-", "_")


def service_relation(
    target: str, relation: str, environ: Mapping[str, str] | None = None
) -> ServiceRelation | None:
    """这条关系配全了才返回；缺任何一项就是没开。"""
    env = os.environ if environ is None else environ
    prefix = f"{_key(target)}_{_key(relation)}_SERVICE_"
    values = {
        "internal_url": env.get(f"{_key(target)}_INTERNAL_URL", "").strip(),
        "client_id": env.get(prefix + "CLIENT_ID", "").strip(),
        "client_secret": env.get(prefix + "CLIENT_SECRET", "").strip(),
        "scope": env.get(prefix + "SCOPE", "").strip(),
        "discovery_url": env.get(prefix + "DISCOVERY_URL", "").strip(),
        "backchannel_endpoint": env.get(prefix + "BACKCHANNEL_ENDPOINT", "").strip(),
    }
    if not all(values.values()) or values["scope"] != f"{target}:{relation}":
        return None
    if urlsplit(values["internal_url"]).scheme != "https":
        return None
    return ServiceRelation(target=target, relation=relation, **values)


class ServiceClient:
    """一条关系的令牌缓存与请求。令牌只在内存里，不落盘、不进日志。"""

    def __init__(
        self,
        relation: ServiceRelation,
        *,
        settings: Settings | None = None,
        oidc_client: Any | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.relation = relation
        self._timeout = timeout
        self._transport = transport
        if oidc_client is None:
            base = settings or get_settings()
            discovery = urlsplit(relation.discovery_url)
            service_settings = base.model_copy(
                update={
                    "casdoor_endpoint": urlunsplit(
                        (discovery.scheme, discovery.netloc, "", "", "")
                    ),
                    "casdoor_discovery_url": relation.discovery_url,
                    "casdoor_backchannel_endpoint": relation.backchannel_endpoint,
                }
            )
            oidc_client = OidcProviderClient(
                service_settings, service_settings.browser_profile("admin")
            )
        self._oidc = oidc_client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        if self._token and self._expires_at > time.time() + 30:
            return self._token
        async with self._lock:
            if self._token and self._expires_at > time.time() + 30:
                return self._token
            body = await self._oidc.exchange_client_credentials(
                scope=self.relation.scope,
                client_id=self.relation.client_id,
                client_secret=self.relation.client_secret,
            )
            token = body.get("access_token") if isinstance(body, dict) else None
            expires_in = body.get("expires_in", 300) if isinstance(body, dict) else 0
            if not isinstance(token, str) or not token:
                raise ServiceUnavailableError(
                    "The service token is missing", code="service_token_unavailable"
                )
            if type(expires_in) not in (int, float) or not 0 < expires_in <= sys.float_info.max:
                raise ServiceUnavailableError(
                    "The service token expiry is invalid",
                    code="service_token_unavailable",
                )
            self._token, self._expires_at = token, time.time() + float(expires_in)
            return token

    async def request(
        self, method: str, path: str, *, json: Any = None
    ) -> tuple[int, Any]:
        if not path.startswith("/") or "//" in path or ".." in path:
            raise ValueError("path must be a plain absolute path")
        url = self.relation.internal_url.rstrip("/") + path
        headers = {
            "Authorization": f"Bearer {await self.token()}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(method, url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                f"{self.relation.target} is unreachable",
                code=f"{self.relation.target}_unavailable",
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 500:
            raise ServiceUnavailableError(
                f"{self.relation.target} failed",
                code=f"{self.relation.target}_unavailable",
            )
        if response.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            raise BadGatewayError(
                f"{self.relation.target} rejected the request",
                code=str(code or f"{self.relation.target}_rejected"),
            )
        return response.status_code, body
