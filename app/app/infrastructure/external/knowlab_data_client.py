"""向 knowlab 读分身绑定的用户数据（SDD 0027 §3.5，服务身份 knowlab:read，契约 knowbot-data/v1）。

工作台按委托核过才来；knowlab 按自己的草稿与交付记录再核一次，不符回 404。环境变量见
service_client.py（`KNOWLAB_INTERNAL_URL`、`KNOWLAB_READ_SERVICE_*`），由部署从后端 config
的 service_clients 生成。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.application.errors.exceptions import BadGatewayError, ServiceUnavailableError
from app.domain.workbench.errors import NotFound, UserDataUnavailable
from app.infrastructure.external.service_client import ServiceClient, service_relation

CONTRACT = "knowbot-data/v1"
DATA = "/knowbot-data"  # 接在 knowlab 入口 /api/internal/v1/knowlab 后面
# 按格子读时 knowlab 的 422：给模型看的话照实说，不带 knowlab 的原文
TABLE_REFUSALS = {
    "knowbot_data_not_a_table": "this data cannot be read as a table; use read_my_data",
    "knowbot_data_no_sheet": "there is no such sheet in this data",
}


class KnowlabDataClient:
    def __init__(self, environ: Mapping[str, str] | None = None, **client_options: Any):
        self._environ = environ
        self._options = client_options
        self._client: ServiceClient | None = None

    def _connect(self) -> ServiceClient:
        if self._client is None:
            relation = service_relation("knowlab", "read", self._environ)
            if relation is None:
                raise UserDataUnavailable("reading the user's data is not configured")
            self._client = ServiceClient(relation, **self._options)
        return self._client

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            _, answer = await self._connect().request(
                "POST", DATA + path, json={"contract": CONTRACT, **body}
            )
        except BadGatewayError as exc:
            if exc.code == "knowbot_data_not_found":
                raise NotFound("data not found") from None
            if exc.code in TABLE_REFUSALS:
                raise UserDataUnavailable(TABLE_REFUSALS[exc.code]) from None
            raise UserDataUnavailable("knowlab refused to read the data") from None
        except ServiceUnavailableError:
            raise UserDataUnavailable("the user's data is unavailable now") from None
        if not isinstance(answer, dict) or answer.get("contract") != CONTRACT:
            raise UserDataUnavailable("knowlab answered outside the contract")
        return answer

    async def list(self, purpose: dict[str, Any]) -> list[dict[str, Any]]:
        answer = await self._post("/list", {"purpose": purpose})
        found = answer.get("datasets")
        if not isinstance(found, list) or not all(isinstance(d, dict) for d in found):
            raise UserDataUnavailable("knowlab answered outside the contract")
        return found

    async def read(
        self, purpose: dict[str, Any], dataset_id: str, page: int
    ) -> dict[str, Any]:
        answer = await self._post(
            "/read", {"purpose": purpose, "dataset_id": dataset_id, "page": page}
        )
        if answer.get("dataset_id") != dataset_id or not isinstance(
            answer.get("text"), str
        ):
            raise UserDataUnavailable("knowlab answered outside the contract")
        answer.pop("contract", None)
        return answer

    async def table(
        self, purpose: dict[str, Any], dataset_id: str, sheet: str | None, page: int
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "purpose": purpose,
            "dataset_id": dataset_id,
            "page": page,
        }
        if sheet:
            body["sheet"] = sheet
        answer = await self._post("/table", body)
        if answer.get("dataset_id") != dataset_id or not isinstance(
            answer.get("rows"), list
        ):
            raise UserDataUnavailable("knowlab answered outside the contract")
        answer.pop("contract", None)
        return answer

    async def cells(
        self, purpose: dict[str, Any], cells: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        answer = await self._post("/cells", {"purpose": purpose, "cells": cells})
        found = answer.get("cells")
        if (
            not isinstance(found, list)
            or len(found) != len(cells)
            or not all(isinstance(c, dict) for c in found)
            or any(
                (c.get("dataset_id"), c.get("sheet"), c.get("row"), c.get("column"))
                != (r["dataset_id"], r["sheet"], r["row"], r["column"])
                for c, r in zip(found, cells, strict=True)
            )
        ):
            raise UserDataUnavailable("knowlab answered outside the contract")
        return found

    async def quotes(
        self, purpose: dict[str, Any], quotes: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        answer = await self._post("/quotes", {"purpose": purpose, "quotes": quotes})
        found = answer.get("quotes")
        if (
            not isinstance(found, list)
            or len(found) != len(quotes)
            or not all(isinstance(q, dict) for q in found)
            or any(
                (q.get("dataset_id"), q.get("page")) != (r["dataset_id"], r["page"])
                for q, r in zip(found, quotes, strict=True)
            )
        ):
            raise UserDataUnavailable("knowlab answered outside the contract")
        return found
