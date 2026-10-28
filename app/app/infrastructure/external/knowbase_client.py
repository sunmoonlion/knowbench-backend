"""调 knowbase 的公共底子：地址、两种身份、拒绝与不可达。

两种身份：
- 用户本人：请求带换来的用户令牌（`AuthService.user_token`，audience 是 knowbase
  在 Casdoor 里的客户端编号，配置 `KNOWBASE_USER_AUDIENCE`）；
  knowbase 按令牌的 `azp` 认是本应用。
- 服务：按跨应用约定的「目标:关系」服务身份，环境变量 `KNOWBASE_INTERNAL_URL`、
  `KNOWBASE_USER_DATA_<关系>_SERVICE_*`（关系：user-data-read），见 service_client.py。
knowbase 回 4xx 抛 KnowbaseRefused（带它的 code，调用方转成自己的话）；不可达、5xx 抛
ServiceUnavailableError。令牌只在内存里，不进日志。具体接口在子类（knowbase_data_v2）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.application.errors.exceptions import ServiceUnavailableError
from app.infrastructure.external.service_client import ServiceClient, service_relation


class KnowbaseRefused(Exception):
    """knowbase 拒了：资料不在、钉的版本不对、授权失效……"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _segment(value: str) -> str:
    return quote(value, safe="")


class KnowbaseClient:
    # 接口的路径前缀，子类定（例如 `/api/internal/v2/user-data`）
    BASE = ""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        **service_options: Any,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._transport = transport
        self._timeout = timeout
        self._service_options = {"transport": transport, **service_options}
        self._services: dict[str, ServiceClient] = {}

    def _base(self) -> str:
        url = self._environ.get("KNOWBASE_INTERNAL_URL", "").strip()
        if urlsplit(url).scheme != "https":
            raise ServiceUnavailableError(
                "knowbase is not configured", code="knowbase_unconfigured"
            )
        return url.rstrip("/") + self.BASE

    async def _service_token(self, relation: str) -> str:
        client = self._services.get(relation)
        if client is None:
            found = service_relation("knowbase", relation, self._environ)
            if found is None:
                raise ServiceUnavailableError(
                    "knowbase is not configured", code="knowbase_unconfigured"
                )
            client = ServiceClient(found, **self._service_options)
            self._services[relation] = client
        return await client.token()

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> tuple[int, Any, httpx.Headers]:
        url = self._base() + path
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method, url, json=json, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                "knowbase is unreachable", code="knowbase_unavailable"
            ) from exc
        if response.status_code >= 500:
            raise ServiceUnavailableError(
                "knowbase failed", code="knowbase_unavailable"
            )
        if response.status_code >= 400:
            try:
                answer = response.json()
            except ValueError:
                answer = None
            code = answer.get("code") if isinstance(answer, dict) else None
            raise KnowbaseRefused(response.status_code, str(code or "knowbase_refused"))
        if response.status_code == 204 or not response.content:
            return response.status_code, None, response.headers
        return response.status_code, response.json(), response.headers
