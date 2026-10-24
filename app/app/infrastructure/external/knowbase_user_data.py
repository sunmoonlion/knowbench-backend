"""knowbase 的用户资料（契约 user-data/v1，SDD 0042）：本应用这一个知识库分区的读写。

两种身份：
- 用户本人：请求带换来的用户令牌（`AuthService.user_token`，audience 是 knowbase
  在 Casdoor 里的客户端编号，配置 `KNOWBASE_USER_AUDIENCE`）；
  knowbase 按令牌的 `azp` 认是本应用的分区。
- 服务：按跨应用约定的「目标:关系」服务身份，环境变量 `KNOWBASE_INTERNAL_URL`、
  `KNOWBASE_USER_DATA_<关系>_SERVICE_*`（关系：user-data-read、user-data-grant-test、
  user-data-purchase），见 service_client.py。
knowbase 回 4xx 抛 KnowbaseRefused（带它的 code，调用方转成自己的话）；不可达、5xx 抛
ServiceUnavailableError。令牌只在内存里，不进日志。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.workbench.pinned_data import CHANGED
from app.domain.workbench.errors import (
    DataChanged,
    DataChoiceInvalid,
    UserDataUnavailable,
)
from app.infrastructure.external.service_client import ServiceClient, service_relation

BASE = "/api/internal/v1/user-data"


class KnowbaseRefused(Exception):
    """knowbase 拒了：资料不在、在用不能删、钉的版本不对、授权失效……"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Content:
    data: bytes
    media_type: str
    sha256: str


def _segment(value: str) -> str:
    return quote(value, safe="")


class KnowbaseUserData:
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

    # —— 用户本人 ——

    async def upload(
        self, user_token: str, name: str, content: bytes
    ) -> dict[str, Any]:
        _, body, _ = await self._send(
            "POST", "/items", user_token, content=content, params={"name": name}
        )
        return body

    async def items(self, user_token: str) -> list[dict[str, Any]]:
        _, body, _ = await self._send("GET", "/items", user_token)
        return body

    async def item(self, user_token: str, item_id: str) -> dict[str, Any]:
        _, body, _ = await self._send("GET", f"/items/{_segment(item_id)}", user_token)
        return body

    async def download(self, user_token: str, item_id: str) -> Content:
        return await self._content(f"/items/{_segment(item_id)}/content", user_token)

    async def delete(self, user_token: str, item_id: str) -> None:
        await self._send("DELETE", f"/items/{_segment(item_id)}", user_token)

    async def consent(
        self, user_token: str, purpose: str, ref: str, items: list[dict[str, str]]
    ) -> dict[str, Any]:
        _, body, _ = await self._send(
            "POST",
            "/consents",
            user_token,
            json={"purpose": purpose, "ref": ref, "items": items},
        )
        return body

    async def withdraw_consent(self, user_token: str, consent_id: str) -> None:
        await self._send("DELETE", f"/consents/{_segment(consent_id)}", user_token)

    async def grant(
        self,
        user_token: str,
        *,
        kind: str,
        reader_app: str,
        ref: str,
        items: list[dict[str, str]],
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": kind,
            "reader_app": reader_app,
            "ref": ref,
            "items": items,
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        _, body, _ = await self._send("POST", "/grants", user_token, json=payload)
        return body

    async def revoke(self, user_token: str, grant_id: str) -> None:
        await self._send("POST", f"/grants/{_segment(grant_id)}/revoke", user_token)

    # —— 服务 ——

    async def test_grant(
        self,
        *,
        owner: str,
        consent_id: str,
        reader_app: str,
        ref: str,
        items: list[dict[str, str]],
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "owner": owner,
            "consent_id": consent_id,
            "reader_app": reader_app,
            "ref": ref,
            "items": items,
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        token = await self._service_token("user-data-grant-test")
        _, body, _ = await self._send("POST", "/test-grants", token, json=payload)
        return body

    async def revoke_test_grant(self, grant_id: str) -> None:
        token = await self._service_token("user-data-grant-test")
        await self._send("POST", f"/test-grants/{_segment(grant_id)}/revoke", token)

    async def view_grant(self, grant_id: str) -> dict[str, Any]:
        token = await self._service_token("user-data-read")
        _, body, _ = await self._send("GET", f"/grants/{_segment(grant_id)}", token)
        return body

    async def release(self, grant_id: str) -> None:
        """读的一方交还给自己的授权（委托结束时）。"""
        token = await self._service_token("user-data-read")
        await self._send("POST", f"/grants/{_segment(grant_id)}/release", token)

    async def read_content(self, grant_id: str, item_id: str) -> Content:
        token = await self._service_token("user-data-read")
        return await self._content(
            f"/grants/{_segment(grant_id)}/items/{_segment(item_id)}/content", token
        )

    async def read_check(self, grant_id: str, item_id: str) -> dict[str, Any]:
        """不取内容，核这个授权还能读这一份：回 item_id、sha256。"""
        token = await self._service_token("user-data-read")
        _, body, _ = await self._send(
            "GET",
            f"/grants/{_segment(grant_id)}/items/{_segment(item_id)}/check",
            token,
        )
        return body

    async def read_format(self, grant_id: str, item_id: str) -> dict[str, Any]:
        token = await self._service_token("user-data-read")
        _, body, _ = await self._send(
            "GET",
            f"/grants/{_segment(grant_id)}/items/{_segment(item_id)}/format",
            token,
        )
        return body

    async def purchase(
        self,
        *,
        seller: str,
        seller_item_id: str,
        buyer: str,
        buyer_app: str,
        transaction_ref: str,
    ) -> dict[str, Any]:
        token = await self._service_token("user-data-purchase")
        _, body, _ = await self._send(
            "POST",
            "/purchases",
            token,
            json={
                "seller": seller,
                "seller_item_id": seller_item_id,
                "buyer": buyer,
                "buyer_app": buyer_app,
                "transaction_ref": transaction_ref,
            },
        )
        return body

    # —— 内部 ——

    def _base(self) -> str:
        url = self._environ.get("KNOWBASE_INTERNAL_URL", "").strip()
        if urlsplit(url).scheme != "https":
            raise ServiceUnavailableError(
                "knowbase is not configured", code="knowbase_unconfigured"
            )
        return url.rstrip("/") + BASE

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

    async def _content(self, path: str, token: str) -> Content:
        _, body, headers = await self._send("GET", path, token, raw=True)
        return Content(
            data=body,
            media_type=headers.get("content-type", "application/octet-stream"),
            sha256=headers.get("x-content-sha256", ""),
        )

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        params: dict[str, str] | None = None,
        raw: bool = False,
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
                    method,
                    url,
                    json=json,
                    content=content,
                    params=params,
                    headers=headers,
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
        if raw:
            return response.status_code, response.content, response.headers
        if response.status_code == 204 or not response.content:
            return response.status_code, None, response.headers
        return response.status_code, response.json(), response.headers


class KnowbaseGrantedData:
    """凭这件委托的运行授权读钉住的那一份（端口 CloudDataReader，SDD 0042 第五节）。

    knowbase 的拒绝转成给模型看的话：资料删了、钉的摘要对不上是「委托开始后改过」；授权不在、
    过期、撤了是读不到（不是专家的错）。
    """

    def __init__(self, client: KnowbaseUserData) -> None:
        self.client = client

    async def content(self, *, grant_id: str, item_id: str) -> bytes:
        try:
            got = await self.client.read_content(grant_id, item_id)
        except (KnowbaseRefused, ServiceUnavailableError) as exc:
            raise _unreadable(exc) from None
        return got.data

    async def check(self, *, grant_id: str, item_id: str) -> str:
        try:
            got = await self.client.read_check(grant_id, item_id)
        except (KnowbaseRefused, ServiceUnavailableError) as exc:
            raise _unreadable(exc) from None
        return str(got.get("sha256") or "") if isinstance(got, dict) else ""


def _unreadable(exc: Exception) -> Exception:
    if isinstance(exc, KnowbaseRefused):
        if exc.code in ("item_not_found", "sha256_mismatch"):
            return DataChanged(CHANGED)
        return UserDataUnavailable(
            f"the permission to read this data has ended ({exc.code})"
        )
    return UserDataUnavailable("the user's data is unavailable now")


class KnowbaseCloudGrants:
    """请专家选云端数据时核资料、签委托运行授权，委托结束交还（端口 CloudGrants，SDD 0042）。"""

    def __init__(self, client: KnowbaseUserData) -> None:
        self.client = client

    async def item(self, user_token: str, item_id: str) -> dict[str, Any]:
        try:
            return await self.client.item(user_token, item_id)
        except KnowbaseRefused as exc:
            if exc.status == 404:
                raise DataChoiceInvalid(
                    "a chosen item is not in your cloud knowledge base"
                ) from None
            raise DataChoiceInvalid(
                f"the cloud knowledge base refused ({exc.code})"
            ) from None
        except ServiceUnavailableError:
            raise UserDataUnavailable(
                "the cloud knowledge base is unavailable now"
            ) from None

    async def grant(
        self,
        user_token: str,
        *,
        ref: str,
        items: list[dict[str, str]],
        ttl_seconds: int,
        kind: str = "delegation",
    ) -> str:
        try:
            body = await self.client.grant(
                user_token,
                kind=kind,
                reader_app="knowbench",
                ref=ref,
                items=items,
                ttl_seconds=ttl_seconds,
            )
        except KnowbaseRefused as exc:
            if exc.code == "sha256_mismatch":
                raise DataChoiceInvalid(
                    "a chosen item changed just now; choose it again"
                ) from None
            raise DataChoiceInvalid(
                f"the cloud knowledge base refused ({exc.code})"
            ) from None
        except ServiceUnavailableError:
            raise UserDataUnavailable(
                "the cloud knowledge base is unavailable now"
            ) from None
        grant_id = body.get("grant_id") if isinstance(body, dict) else None
        if not isinstance(grant_id, str) or not grant_id:
            raise UserDataUnavailable(
                "the cloud knowledge base answered outside the contract"
            )
        return grant_id

    async def release(self, grant_id: str) -> None:
        try:
            await self.client.release(grant_id)
        except KnowbaseRefused as exc:
            if exc.status == 404:
                return  # 已经没有了：与交还了一样
            raise UserDataUnavailable(
                f"handing back the grant failed ({exc.code})"
            ) from None
        except ServiceUnavailableError:
            raise UserDataUnavailable(
                "the cloud knowledge base is unavailable now"
            ) from None
