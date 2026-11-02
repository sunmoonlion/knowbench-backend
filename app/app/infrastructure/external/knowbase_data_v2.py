"""knowbase 资料库第四版（契约 user-data/v2，SDD 0043、0048）：请专家的解析、确认、
运行授权，会话登记，等数据时的服务路，数据 MCP 的中继。

两种身份、拒绝与不可达见公共底子 `knowbase_client.py`：本人令牌（换来的用户令牌，`azp`
是本应用），服务身份走关系 `user-data-read`；委托交付的成稿替用户写进产出区走关系
`user-data-output`（SDD 0043 第 2.6 节）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.application.errors.exceptions import ServiceUnavailableError
from app.infrastructure.external.knowbase_client import KnowbaseClient, _segment

BASE_V2 = "/api/internal/v2/user-data"
DATA_MCP_PATH = "/api/mcp/data"
READ = "user-data-read"
OUTPUT = "user-data-output"
log = logging.getLogger(__name__)


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

    async def save_preference(
        self,
        user_token: str,
        *,
        consumer: str,
        key: dict[str, Any],
        material_ids: list[str],
    ) -> dict[str, Any] | None:
        """用户在确认面板上把 knowbase 挑的那份换成另一份：记成偏好（用户 × 使用者 ×
        描述 → 资料，SDD 0043 第 3.5 节）。凭本人令牌；只是下次解析的排序输入。"""
        _, body, _ = await self._send(
            "PUT",
            "/preferences",
            user_token,
            json={"consumer": consumer, "key": key, "material_ids": material_ids},
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

    async def refresh_mine(self, user_token: str, resolution_id: str) -> dict[str, Any]:
        """本人在场的重解析（「传好了，重新查」）：已定的不动，没定的重找；形状同服务那条。"""
        _, body, _ = await self._send(
            "POST", f"/resolutions/{_segment(resolution_id)}/refresh", user_token
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

    # —— 服务：替用户写产出区（SDD 0043 第 2.6 节）——

    async def write_output(
        self,
        *,
        beneficiary: str,
        task_ref: str,
        run_ref: str,
        name: str,
        content: bytes,
        expert_id: str | None = None,
        expert_version: str | None = None,
        grant_ref: str | None = None,
        task_started_at: str | None = None,
        task_finished_at: str | None = None,
        incomplete: bool = False,
    ) -> dict[str, Any]:
        """只能写进受益用户的产出区；同一次运行同名的重放回原来那份，内容不同 409。
        回 {material_id, version_no, sha256}。

        上游许可的依据（独立审 10-30 两轮定）：`grant_ref` 是这件事的运行授权 ref，knowbase
        按这个 ref 下签过的授权（含已交还、过期的）与读数留痕算升档上限，ref 下没有任何读取
        就不设上限；不带的按「只许本人用」。回包回显认下的依据 `provenance_basis`。
        `task_started_at`、`task_finished_at`（ISO8601）：这件委托的起止，knowbase 把这段
        时间里受益人经 knowbench 会话授权读过的资料也算进上限（工作台 MCP 的令牌分不出
        调用来自对话还是委托步骤，委托里的模型可能借别的对话读了数据，宁严勿松）。
        `incomplete=true`：必需的数据缺了、用户选了照做（0043 第 3.6 节）。"""
        params: dict[str, str] = {
            "beneficiary": beneficiary,
            "task_ref": task_ref,
            "run_ref": run_ref,
            "name": name,
        }
        given = {
            "expert_id": expert_id,
            "expert_version": expert_version,
            "grant_ref": grant_ref,
            "task_started_at": task_started_at,
            "task_finished_at": task_finished_at,
        }
        params |= {k: v for k, v in given.items() if v}
        if incomplete:
            params["incomplete"] = "true"
        _, body, _ = await self._send(
            "POST",
            "/outputs",
            await self._service_token(OUTPUT),
            params=params,
            content=content,
        )
        return body

    async def find_output(
        self, *, beneficiary: str, task_ref: str, run_ref: str, name: str
    ) -> dict[str, Any]:
        """这次运行同名已经写过的那份（写的时候回了 409 `output_exists`，查回来记成已写）：
        {material_id, version_no, sha256}。"""
        _, body, _ = await self._send(
            "GET",
            "/outputs/lookup",
            await self._service_token(OUTPUT),
            params={
                "beneficiary": beneficiary,
                "task_ref": task_ref,
                "run_ref": run_ref,
                "name": name,
            },
        )
        return body

    # —— 数据 MCP 中继（0048）——

    @property
    def data_mcp_configured(self) -> bool:
        return bool(self._environ.get("KNOWBASE_INTERNAL_URL", "").strip())

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
        if response.status_code == 202 and "id" not in message:
            return {}
        if response.status_code != 200:
            log.warning("knowbase_data_mcp_http_error status=%s", response.status_code)
            raise ServiceUnavailableError(
                f"knowbase data MCP returned HTTP {response.status_code}",
                code="knowbase_mcp_http_error",
            )
        try:
            reply = response.json()
        except ValueError:
            reply = None
        if (
            not isinstance(reply, dict)
            or reply.get("jsonrpc") != "2.0"
            or reply.get("id") != message.get("id")
            or ("result" in reply) == ("error" in reply)
            or not isinstance(reply.get("result", reply.get("error")), dict)
        ):
            raise ServiceUnavailableError(
                "knowbase data MCP returned an invalid response",
                code="knowbase_mcp_invalid_response",
            )
        return reply
