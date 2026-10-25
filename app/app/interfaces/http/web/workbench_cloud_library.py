"""云端知识库（SDD 0042）：用户上传的、代采取回的、从 knowmart 买来的资料。

真源在 knowbase 的 knowbench 分区；这里只转交，凭用户本人的令牌（会话里留的访问令牌换来的），
knowbase 按令牌认人、认分区。专家交回的底稿不在这里（产出不是知识库，10-23）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response

from app.application.errors.exceptions import AppException
from app.infrastructure.external.knowbase_user_data import (
    KnowbaseRefused,
    KnowbaseUserData,
)
from app.interfaces.endpoints.workbench_routes import (
    CONTRACT_VERSION,
    require_workbench_enabled,
)
from app.interfaces.http.middleware.auth import get_web_knowbase_token

router = APIRouter(
    prefix="/workbench",
    tags=["Workbench"],
    dependencies=[Depends(require_workbench_enabled)],
)

# 与 knowbase 一致：单份 50 MB；超了不收完就拒
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_client: KnowbaseUserData | None = None


def get_knowbase() -> KnowbaseUserData:
    global _client
    if _client is None:
        _client = KnowbaseUserData()
    return _client


def _refused(exc: KnowbaseRefused) -> AppException:
    # knowbase 的 code 原样给网页，网页按 code 说人话
    status = exc.status if exc.status in (401, 403, 404, 409, 413, 422) else 502
    return AppException(code=exc.code, status_code=status, msg=exc.code)


def _item(raw: dict[str, Any]) -> dict[str, Any]:
    keep = ("item_id", "name", "media_type", "size_bytes", "sha256", "origin")
    return {
        **{k: raw.get(k) for k in keep},
        "created_at": raw.get("created_at"),
        # 买来、卖家写明不许转卖的（Fable 评审 P2-16）
        "no_resale": raw.get("no_resale") is True,
    }


@router.get("/cloud-library")
async def cloud_library_listing(
    token: str = Depends(get_web_knowbase_token),
    knowbase: KnowbaseUserData = Depends(get_knowbase),
) -> dict[str, Any]:
    try:
        items = await knowbase.items(token)
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    return {"contract_version": CONTRACT_VERSION, "items": [_item(i) for i in items]}


@router.post("/cloud-library", status_code=201)
async def cloud_library_upload(
    request: Request,
    name: str = Query(min_length=1, max_length=200),
    token: str = Depends(get_web_knowbase_token),
    knowbase: KnowbaseUserData = Depends(get_knowbase),
) -> dict[str, Any]:
    """请求体就是文件本身。"""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise AppException(code="too_large", status_code=413, msg="too_large")
        chunks.append(chunk)
    try:
        item = await knowbase.upload(token, name, b"".join(chunks))
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    return {"contract_version": CONTRACT_VERSION, "item": _item(item)}


@router.get("/cloud-library/{item_id}/content")
async def cloud_library_download(
    item_id: str,
    token: str = Depends(get_web_knowbase_token),
    knowbase: KnowbaseUserData = Depends(get_knowbase),
) -> Response:
    try:
        meta = await knowbase.item(token, item_id)
        content = await knowbase.download(token, item_id)
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    filename = quote(str(meta.get("name") or "download"), safe="")
    return Response(
        content=content.data,
        media_type=content.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Content-SHA256": content.sha256,
            "Cache-Control": "no-store",
        },
    )


@router.delete("/cloud-library/{item_id}", status_code=204)
async def cloud_library_delete(
    item_id: str,
    token: str = Depends(get_web_knowbase_token),
    knowbase: KnowbaseUserData = Depends(get_knowbase),
) -> Response:
    try:
        await knowbase.delete(token, item_id)
    except KnowbaseRefused as exc:
        raise _refused(exc) from None
    return Response(status_code=204)
