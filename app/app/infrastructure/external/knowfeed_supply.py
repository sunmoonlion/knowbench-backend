"""knowfeed 的需求单（SDD 0045 第 2.4 节）：请专家要补采的，凭本人的确认编号提单、按单号
查进度、取消。

服务身份走关系 `knowfeed:supply-demand`（环境变量 `KNOWFEED_INTERNAL_URL`、
`KNOWFEED_SUPPLY_DEMAND_SERVICE_*`，见 service_client.py）；knowfeed 按关系校验认出这是
knowbench。`KNOWFEED_INTERNAL_URL` 已经带着内部前缀
（`…/api/internal/v1/supply`，部署机制的约定），后面直接接 `/demands`、`/securities`。需求状态 knowfeed 会推过来（契约
knowfeed-supply-status/v1，接收端 `interfaces/endpoints/knowfeed_status_routes.py`），按单号
查留着兜底。
没配就抛 ServiceUnavailableError（code `knowfeed_unconfigured`），调用方据此说「暂不能补采」。

证券名录（`securities`）：委托第 0 步模型「查主体」，按名字查证券代码（decisions ⑭）；
同一个服务身份，不看补采开关。

预切合（`prematch`，契约 knowfeed-supply-prematch/v1，`domain/workbench/supply_prematch.py`）：
确认之前问待确认的几项能不能取、许可档、预计多久；同一个服务身份与 scope。它只查来源目录、
毫秒级，确认面板每次取数据都问，所以超时设得短，问不到面板照常列、只是不显示预计。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.application.errors.exceptions import ServiceUnavailableError
from app.domain.workbench.supply_prematch import PrematchResponse
from app.infrastructure.external.service_client import ServiceClient, service_relation

# 预切合只查来源目录（0045 第四节「毫秒级」）：问不到不挡面板
PREMATCH_TIMEOUT = 5.0


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

    async def source_tools(
        self, owner: str, service: str | None = None
    ) -> dict[str, Any]:
        params = {"owner": owner}
        if service:
            params["service"] = service
        return await self._send("GET", "/source-tools", params=params)

    async def securities(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """按名字或代码查证券：[{subject, name}]，对得上的在前。"""
        found = await self._send(
            "GET", "/securities", params={"q": query, "limit": str(limit)}
        )
        return found if isinstance(found, list) else []

    async def prematch(
        self, items: list[dict[str, Any]], conditions: dict[str, Any]
    ) -> dict[str, Any]:
        """回包照提供方的模型核过；对不上当作调不通。"""
        answer = await self._send(
            "POST",
            "/prematch",
            json={"items": items, "conditions": conditions},
            limit_seconds=PREMATCH_TIMEOUT,
        )
        try:
            return PrematchResponse.model_validate(answer).model_dump(mode="json")
        except ValidationError as exc:
            raise ServiceUnavailableError(
                "knowfeed answered off contract", code="knowfeed_unavailable"
            ) from exc

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
        limit_seconds: float | None = None,
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
                timeout=limit_seconds or self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    relation.internal_url.rstrip("/") + path,
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
