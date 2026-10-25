"""云端知识库（SDD 0042）：网页经后端、凭用户令牌转给 knowbase 的 knowbench 分区。"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.application.errors.exceptions import ServiceUnavailableError, UnauthorizedError
from app.infrastructure.external.knowbase_user_data import Content, KnowbaseRefused
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.http.web import workbench_cloud_library as cloud

ITEM = {
    "item_id": "i1",
    "app": "knowbench",
    "name": "茅台 2023 年报.pdf",
    "media_type": "application/pdf",
    "size_bytes": 6,
    "sha256": "ab" * 32,
    "origin": "upload",
    "origin_ref": None,
    "created_at": "2026-10-23T12:00:00Z",
}


class FakeKnowbase:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.refuse: KnowbaseRefused | None = None

    def _maybe_refuse(self) -> None:
        if self.refuse is not None:
            raise self.refuse

    async def items(self, token: str) -> list[dict]:
        self.calls.append(("items", token))
        self._maybe_refuse()
        return [ITEM]

    async def upload(self, token: str, name: str, content: bytes) -> dict:
        self.calls.append(("upload", token, name, content))
        self._maybe_refuse()
        return {**ITEM, "name": name, "size_bytes": len(content)}

    async def item(self, token: str, item_id: str) -> dict:
        self.calls.append(("item", token, item_id))
        self._maybe_refuse()
        return ITEM

    async def download(self, token: str, item_id: str) -> Content:
        self.calls.append(("download", token, item_id))
        return Content(data=b"%PDF-1", media_type="application/pdf", sha256="ab" * 32)

    async def delete(self, token: str, item_id: str) -> None:
        self.calls.append(("delete", token, item_id))
        self._maybe_refuse()


@pytest.fixture
def api():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cloud.router, prefix="/api")
    knowbase = FakeKnowbase()
    state: dict[str, object] = {"token": "user-token"}

    def token() -> str:
        if isinstance(state["token"], Exception):
            raise state["token"]
        return str(state["token"])

    app.dependency_overrides[cloud.require_workbench_enabled] = lambda: None
    app.dependency_overrides[cloud.get_web_knowbase_token] = token
    app.dependency_overrides[cloud.get_knowbase] = lambda: knowbase
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bench"
    )
    return client, knowbase, state


async def test_listing_shows_the_users_items_without_internal_fields(api):
    client, knowbase, _ = api
    body = (await client.get("/api/workbench/cloud-library")).json()
    assert [i["name"] for i in body["items"]] == ["茅台 2023 年报.pdf"]
    assert "origin_ref" not in body["items"][0] and "app" not in body["items"][0]
    assert body["items"][0]["no_resale"] is False  # 买来不许转卖的才为真（Fable P2-16）
    assert knowbase.calls == [("items", "user-token")]


async def test_upload_passes_the_file_and_name_through(api):
    client, knowbase, _ = api
    created = await client.post(
        "/api/workbench/cloud-library", params={"name": "营收.csv"}, content=b"a,b\n"
    )
    assert created.status_code == 201
    assert created.json()["item"]["name"] == "营收.csv"
    assert knowbase.calls == [("upload", "user-token", "营收.csv", b"a,b\n")]


async def test_oversized_uploads_stop_before_knowbase(api, monkeypatch):
    client, knowbase, _ = api
    monkeypatch.setattr(cloud, "MAX_UPLOAD_BYTES", 4)
    refused = await client.post(
        "/api/workbench/cloud-library", params={"name": "a.csv"}, content=b"12345"
    )
    assert (refused.status_code, refused.json()["code"]) == (413, "too_large")
    assert knowbase.calls == []


async def test_download_is_an_attachment_named_after_the_item(api):
    client, _, _ = api
    got = await client.get("/api/workbench/cloud-library/i1/content")
    assert got.content == b"%PDF-1"
    assert got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''%E8%8C%85%E5%8F%B0%202023%20%E5%B9%B4%E6%8A%A5.pdf"
    )
    assert got.headers["x-content-sha256"] == "ab" * 32


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (409, "in_use", 409),
        (404, "item_not_found", 404),
        (422, "type_not_allowed", 422),
        (413, "quota_exceeded", 413),
        (418, "odd", 502),
    ],
)
async def test_knowbase_refusals_keep_their_code(api, status, code, expected):
    client, knowbase, _ = api
    knowbase.refuse = KnowbaseRefused(status, code)
    answer = await client.delete("/api/workbench/cloud-library/i1")
    assert (answer.status_code, answer.json()["code"]) == (expected, code)


async def test_sign_in_again_and_unconfigured_reach_the_page(api):
    client, _, state = api
    state["token"] = UnauthorizedError("Sign in again", code="reauth_required")
    answer = await client.get("/api/workbench/cloud-library")
    assert (answer.status_code, answer.json()["code"]) == (401, "reauth_required")
    state["token"] = ServiceUnavailableError("x", code="knowbase_unconfigured")
    answer = await client.get("/api/workbench/cloud-library")
    assert (answer.status_code, answer.json()["code"]) == (503, "knowbase_unconfigured")
