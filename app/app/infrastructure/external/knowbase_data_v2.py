"""knowbase 资料库第四版（契约 user-data/v2，SDD 0043、0048）：请专家的解析、确认、
运行授权，会话登记，等数据时的服务路，数据 MCP 的中继。

两种身份、拒绝与不可达见公共底子 `knowbase_client.py`：本人令牌（换来的用户令牌，`azp`
是本应用），服务身份走关系 `user-data-read`。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.application.errors.exceptions import ServiceUnavailableError
from app.infrastructure.external.knowbase_client import KnowbaseClient, _segment

BASE_V2 = "/api/internal/v2/user-data"
DATA_MCP_PATH = "/api/mcp/data"
READ = "user-data-read"


class KnowbaseDataV2(KnowbaseClient):
    BASE = BASE_V2

    # —— 本人 ——

    async def resolve(
        self,
        user_token: str,
        *,
        consumer: str,
        needs: list[dict[str, Any]],
        params: dict[str, Any],
        task_ref: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "consumer": consumer,
            "contract_version": "data-request/v1",
            "needs": needs,
            "params": params,
        }
        if task_ref is not None:
            payload["task_ref"] = task_ref
        _, body, _ = await self._send("POST", "/resolutions", user_token, json=payload)
        return body

    async def resolution(
        self, user_token: str, resolution_id: str, revision: int | None = None
    ) -> dict[str, Any]:
        _, body, _ = await self._send(
            "GET",
            f"/resolutions/{_segment(resolution_id)}",
            user_token,
            params={"revision": str(revision)} if revision else None,
        )
        return body

    async def revise_needs(
        self, user_token: str, resolution_id: str, needs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """用户确认前改了清单（换主体、改期间、去掉、加一样）：整份换掉，出新修订。"""
        _, body, _ = await self._send(
            "PUT",
            f"/resolutions/{_segment(resolution_id)}/needs",
            user_token,
            json={"needs": needs},
        )
        return body

    async def confirm(
        self,
        user_token: str,
        *,
        items: list[dict[str, Any]],
        task_ref: str,
        resolution_id: str,
        revision: int,
        supply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, body, _ = await self._send(
            "POST",
            "/confirmations",
            user_token,
            json={
                "kind": "description",
                "items": items,
                "supply": supply or {},
                "task_ref": task_ref,
                "resolution_id": resolution_id,
                "revision": revision,
            },
        )
        return body

    async def run_grant(
        self,
        user_token: str,
        *,
        process: str,
        ref: str,
        items: list[dict[str, Any]],
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"process": process, "ref": ref, "items": items}
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        _, body, _ = await self._send("POST", "/run-grants", user_token, json=payload)
        return body

    async def register_session(self, user_token: str) -> dict[str, Any]:
        _, body, _ = await self._send("POST", "/sessions", user_token)
        return body

    # —— 服务：执行的应用 ——

    async def service_resolution(
        self, subject: str, resolution_id: str, revision: int | None = None
    ) -> dict[str, Any]:
        params = {"subject": subject}
        if revision:
            params["revision"] = str(revision)
        _, body, _ = await self._send(
            "GET",
            f"/service/resolutions/{_segment(resolution_id)}",
            await self._service_token(READ),
            params=params,
        )
        return body

    async def refresh(self, subject: str, resolution_id: str) -> dict[str, Any]:
        """数据到了：重解析清单（已定的不动，没变就回当前修订）。"""
        _, body, _ = await self._send(
            "POST",
            f"/service/resolutions/{_segment(resolution_id)}/refresh",
            await self._service_token(READ),
            params={"subject": subject},
        )
        return body

    async def add_needs(
        self, subject: str, resolution_id: str, needs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """运行中的专家要新数据（request_data）：只出清单新修订，确认仍要本人。"""
        _, body, _ = await self._send(
            "POST",
            f"/service/resolutions/{_segment(resolution_id)}/revisions",
            await self._service_token(READ),
            params={"subject": subject},
            json={"needs": needs},
        )
        return body

    async def grant_by_confirmation(
        self,
        *,
        subject: str,
        confirmation_id: str,
        resolution_id: str,
        revision: int,
        ref: str,
        items: list[dict[str, Any]],
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """等数据的委托数据齐了：凭本人那次确认续签（所有者 10-27 定）。"""
        payload: dict[str, Any] = {
            "subject": subject,
            "confirmation_id": confirmation_id,
            "resolution_id": resolution_id,
            "revision": revision,
            "ref": ref,
            "items": items,
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        _, body, _ = await self._send(
            "POST",
            "/service/run-grants/by-confirmation",
            await self._service_token(READ),
            json=payload,
        )
        return body

    async def release_run_grant(self, grant_id: str) -> None:
        await self._send(
            "POST",
            f"/run-grants/{_segment(grant_id)}/release",
            await self._service_token(READ),
        )

    async def release_run(self, ref: str) -> None:
        """委托、会话结束：这件事签过的全部交还。"""
        await self._send(
            "POST", f"/runs/{_segment(ref)}/release", await self._service_token(READ)
        )

    # —— 数据 MCP 中继（0048）——

    async def data_mcp(self, token: str, message: dict[str, Any]) -> dict[str, Any]:
        """把一条 JSON-RPC 原样交给 knowbase 数据 MCP；令牌是工作台当场签的
        `knowbase-data` 令牌。只回 JSON（knowbase 不开流）。"""
        url = self._base().removesuffix(BASE_V2) + DATA_MCP_PATH
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    url,
                    json=message,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                "knowbase is unreachable", code="knowbase_unavailable"
            ) from exc
        if response.status_code >= 500 or response.status_code == 401:
            raise ServiceUnavailableError(
                "knowbase data tools failed", code="knowbase_unavailable"
            )
        if response.status_code == 202 or not response.content:
            return {}
        return response.json()
