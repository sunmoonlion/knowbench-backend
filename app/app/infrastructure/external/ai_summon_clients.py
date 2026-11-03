"""AI 请专家要调的三条服务关系（请专家方案设计第 4.9 节；decisions 10-29「AI 请专家」）。

- `knowmart:catalog`：替用户「找专家」，取这个用户请得动、运行方是 knowbench 的专家的公开信息
  （名称、一句话、接什么 / 不接什么 / 不接的去哪问、版本、价格、免不免费请）；
- `knowmart:demand`：用户看过描述、点了「发布」后，替他在 knowmart 挂需求；
- `knowlab:incubation-request`：用户点了「孵化一个专家」，knowlab 以他的名义建一份「问题」申请。

环境变量见 service_client.py：`KNOWMART_INTERNAL_URL`（带内部前缀 `…/api/internal/v1/knowmart`）
与 `KNOWMART_CATALOG_SERVICE_*`、`KNOWMART_DEMAND_SERVICE_*`；`KNOWLAB_INTERNAL_URL`（带内部前缀
`…/api/internal/v1/knowlab`）与 `KNOWLAB_INCUBATION_REQUEST_SERVICE_*`。没配就是没开。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from app.application.errors.exceptions import BadGatewayError, ServiceUnavailableError
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.expert_match import DirectorySnapshot
from app.infrastructure.external.service_client import ServiceClient, service_relation


class AiSummonUnavailable(WorkbenchError):
    """那一头没配、连不上或答得不对。"""

    code = "ai_summon_unavailable"
    http_status = 503


class _Relation:
    def __init__(
        self,
        target: str,
        relation: str,
        environ: Mapping[str, str] | None = None,
        **client_options: Any,
    ) -> None:
        self._target, self._relation = target, relation
        self._environ = environ
        self._options = client_options
        self._client: ServiceClient | None = None

    def configured(self) -> bool:
        return service_relation(self._target, self._relation, self._environ) is not None

    def _connect(self) -> ServiceClient:
        if self._client is None:
            found = service_relation(self._target, self._relation, self._environ)
            if found is None:
                raise AiSummonUnavailable(
                    f"{self._target}:{self._relation} is not configured"
                )
            self._client = ServiceClient(found, **self._options)
        return self._client

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            _, body = await self._connect().request(method, path, **kwargs)
        except (BadGatewayError, ServiceUnavailableError) as exc:
            raise AiSummonUnavailable(
                f"{self._target} refused: {getattr(exc, 'code', 'error')}"
            ) from None
        return body


class KnowmartCatalog(_Relation):
    def __init__(self, environ: Mapping[str, str] | None = None, **options: Any):
        super().__init__("knowmart", "catalog", environ, **options)

    async def experts(self, owner_subject: str) -> list[dict[str, Any]]:
        body = await self._call(
            "GET", f"/owners/{quote(owner_subject, safe='')}/catalog"
        )
        experts = body.get("experts") if isinstance(body, dict) else None
        if not isinstance(experts, list):
            raise AiSummonUnavailable("knowmart answered outside the contract")
        return [e for e in experts if isinstance(e, dict) and e.get("knowbot_id")]

    async def snapshot(self, owner_subject: str) -> DirectorySnapshot:
        body = await self._call(
            "GET",
            f"/owners/{quote(owner_subject, safe='')}/catalog?directory_version=2",
        )
        try:
            snapshot = DirectorySnapshot.model_validate(body)
        except ValueError:
            raise AiSummonUnavailable("knowmart public directory is invalid") from None
        if not snapshot.complete:
            raise AiSummonUnavailable("knowmart public directory is incomplete")
        return snapshot


class KnowmartDemands(_Relation):
    def __init__(self, environ: Mapping[str, str] | None = None, **options: Any):
        super().__init__("knowmart", "demand", environ, **options)

    async def publish(
        self, owner_subject: str, demand: dict[str, Any]
    ) -> dict[str, Any]:
        body = await self._call(
            "POST", f"/owners/{quote(owner_subject, safe='')}/demands", json=demand
        )
        found = body.get("demand") if isinstance(body, dict) else None
        if not isinstance(found, dict):
            raise AiSummonUnavailable("knowmart answered outside the contract")
        return found


class KnowlabIncubationRequests(_Relation):
    def __init__(self, environ: Mapping[str, str] | None = None, **options: Any):
        super().__init__("knowlab", "incubation-request", environ, **options)

    async def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        body = await self._call("POST", "/incubation-requests", json=request)
        if not isinstance(body, dict) or not isinstance(body.get("request_id"), str):
            raise AiSummonUnavailable("knowlab answered outside the contract")
        return body
