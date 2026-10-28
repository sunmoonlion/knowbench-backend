"""knowfeed 的需求单（SDD 0045 第 2.4 节）：请专家要补采的，凭本人的确认编号提单、按单号
查进度、取消。

服务身份走关系 `knowfeed:supply-demand`（环境变量 `KNOWFEED_INTERNAL_URL`、
`KNOWFEED_SUPPLY_DEMAND_SERVICE_*`，见 service_client.py）；knowfeed 按部署配的
`SUPPLY_SERVICE_APPS_JSON` 认这是 knowbench。需求状态的推送 knowfeed 还没做，现在按单号查。
没配就抛 ServiceUnavailableError（code `knowfeed_unconfigured`），调用方据此说「暂不能补采」。

证券名录（`securities`）：委托第 0 步模型「查主体」，按名字查证券代码（decisions ⑭）；
同一个服务身份，不看补采开关。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from app.application.errors.exceptions import ServiceUnavailableError
from app.infrastructure.external.service_client import ServiceClient, service_relation

BASE = "/api/internal/v1/supply"


class KnowfeedRefused(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class KnowfeedSupply:
    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        **service_options: Any,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._transport = transport
        self._timeout = timeout
        self._service_options = {"transport": transport, **service_options}
        self._client: ServiceClient | None = None

    def configured(self) -> bool:
        return service_relation("knowfeed", "supply-demand", self._environ) is not None

    async def submit(
        self,
        *,
        owner: str,
        confirmation_id: str,
        idempotency_key: str,
        items: list[dict[str, Any]],
        task_ref: str | None,
        proposer: str = "user",
        chain: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._send(
            "POST",
            "/demands",
            json={
                "owner": owner,
                "proposer": proposer,
                "chain": chain or [],
                "task_ref": task_ref,
                "confirmation_id": confirmation_id,
                "idempotency_key": idempotency_key,
                "items": items,
            },
        )

    async def get(self, demand_id: str) -> dict[str, Any]:
        return await self._send("GET", f"/demands/{quote(demand_id, safe='')}")

    async def cancel(self, demand_id: str) -> dict[str, Any]:
        return await self._send("POST", f"/demands/{quote(demand_id, safe='')}/cancel")

    async def securities(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """按名字或代码查证券：[{subject, name}]，对得上的在前。"""
        found = await self._send(
            "GET", "/securities", params={"q": query, "limit": str(limit)}
        )
        return found if isinstance(found, list) else []

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        relation = service_relation("knowfeed", "supply-demand", self._environ)
        if relation is None:
            raise ServiceUnavailableError(
                "knowfeed is not configured", code="knowfeed_unconfigured"
            )
        if self._client is None:
            self._client = ServiceClient(relation, **self._service_options)
        token = await self._client.token()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    relation.internal_url.rstrip("/") + BASE + path,
                    json=json,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                "knowfeed is unreachable", code="knowfeed_unavailable"
            ) from exc
        if response.status_code >= 500:
            raise ServiceUnavailableError(
                "knowfeed failed", code="knowfeed_unavailable"
            )
        if response.status_code >= 400:
            try:
                answer = response.json()
            except ValueError:
                answer = None
            code = answer.get("code") if isinstance(answer, dict) else None
            raise KnowfeedRefused(response.status_code, str(code or "knowfeed_refused"))
        return response.json()
