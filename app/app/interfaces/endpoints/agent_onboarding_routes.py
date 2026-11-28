"""Public pairing/install-capability endpoints and authenticated owner actions."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.application.workbench.agent_download import (
    InvalidRange,
    ReleaseIdentity,
    ReleaseUnavailable,
    byte_range,
)
from app.application.workbench.agent_onboarding import (
    INSTALL_TTL,
    AgentOnboarding,
    install_digest,
    new_install_credential,
)
from app.domain.security import Principal
from app.domain.workbench.errors import (
    AgentInstallUnavailable,
    AgentPairingSlowDown,
    WorkbenchError,
)
from app.infrastructure.storage.postgres import get_db_session
from app.infrastructure.storage.redis import get_redis
from app.infrastructure.workbench.agent_limiter import (
    allow_install_issue,
    allow_pairing_ip,
    allow_user_lookup,
    allow_user_lookup_failure,
)
from app.interfaces.endpoints.workbench_routes import (
    _actor,
    _http,
    _PackageResponse,
    _provisioning,
    credential_cipher,
    provisioning_backends,
    require_workbench_enabled,
    token_issuer,
    workbench_store,
)
from app.interfaces.http.client_ip import source_ip
from app.interfaces.http.middleware.auth import get_web_current_user
from core.config import AgentDownload, get_settings

public_router = APIRouter(tags=["Agent onboarding"])
owner_router = APIRouter(tags=["Agent onboarding"])
logger = logging.getLogger(__name__)
# 设置分三类后，接入电脑是第三类（0021）；旧的 `settings#computer` 由网页转过去
VERIFY_PATH = "/zh-CN/workbench/settings/computer"
VERIFY_ANCHOR = "#computer"


def pairing_verify_url(web_frontend_base_url: str, user_code: str | None = None) -> str:
    """用户允许这台电脑的那一页。

    带 `code` 时网页自动填码、自动核对，用户只剩看一眼机器名再点「允许」
    （所有者 2026-10-12 实测：两个标签一前一后弹出来，用户不知道码要誊到哪）。码本身
    5 分钟一次性、还要用户点允许才生效，进地址栏可以接受；令牌从不进地址。
    """
    base = web_frontend_base_url.rstrip("/") + VERIFY_PATH
    if user_code:
        base += "?" + urlencode({"code": user_code})
    return base + VERIFY_ANCHOR


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreatePairing(StrictBody):
    machine_name: str = Field(min_length=1, max_length=128)
    os: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(min_length=1, max_length=64)
    codex_version: str = Field(min_length=1, max_length=64)
    device_secret_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64
    )

    @field_validator("machine_name", "os", "agent_version", "codex_version")
    @classmethod
    def printable(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(c) < 32 or ord(c) == 127 for c in normalized):
            raise ValueError("value must be printable")
        return normalized


class ExpectedInstallRelease(StrictBody):
    version: str = Field(min_length=1, max_length=128)
    zip_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeviceSecret(StrictBody):
    model_config = ConfigDict(extra="forbid", strict=True)
    device_secret: str = Field(
        min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )


class PairingLookup(StrictBody):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_code: str = Field(min_length=8, max_length=9)


class PairingApprove(StrictBody):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_code: str = Field(min_length=8, max_length=9)


def _onboarding(repo, cipher, provisioning=None) -> AgentOnboarding:
    key = get_settings().workbench_agent_pairing_hmac_key
    if key is None:
        raise HTTPException(status_code=503, detail="agent_pairing_unavailable")
    return AgentOnboarding(
        repo,
        cipher=cipher,
        pairing_hmac_key=key.get_secret_value().encode(),
        provisioning=provisioning,
    )


def _peer(request: Request) -> str | None:
    return request.client.host if request.client else None


def _code_as_not_found(exc: WorkbenchError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.message)


def _download_ready(settings) -> AgentDownload:
    release = settings.workbench_agent_download
    if (
        release is None
        or release.mode != "object-storage"
        or not release.object_key
        or settings.workbench_agent_release_storage is None
        or not settings.workbench_agent_install_script_template
    ):
        raise AgentInstallUnavailable("agent download is not available")
    assert release.object_key is not None
    return release


async def _issued_release(repo, credential: str) -> tuple[AgentDownload, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", credential):
        raise AgentInstallUnavailable("agent download is not available")
    async with repo.transaction():
        snapshot = await repo.agent_install_release(
            install_digest(credential), now=datetime.now(UTC)
        )
    if not snapshot:
        raise AgentInstallUnavailable("agent download is not available")
    return AgentDownload.model_validate(snapshot["release"]), snapshot["template"]


async def _consume_install_credential(repo, credential: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", credential):
        raise AgentInstallUnavailable("agent download is not available")
    async with repo.transaction():
        row = await repo.consume_agent_install_credential(
            install_digest(credential), now=datetime.now(UTC)
        )
    if row is None:
        raise AgentInstallUnavailable("agent download is not available")
    return row


@public_router.post("/agent-pairing/requests")
async def create_pairing_request(
    body: CreatePairing,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
):
    require_workbench_enabled()
    settings = get_settings()
    peer = _peer(request)
    ip = source_ip(
        peer,
        request.headers.getlist("x-forwarded-for"),
        settings.workbench_trusted_proxy_cidrs,
    )
    try:
        if not await allow_pairing_ip(get_redis().client, ip):
            raise AgentPairingSlowDown("too many pairing requests")
        repo = workbench_store(session)
        service = _onboarding(repo, cipher)
        base_url = settings.web_frontend_base_url

        def verify_url(user_code: str) -> str:
            return pairing_verify_url(base_url, user_code)

        async with repo.transaction():
            result = await service.create_request(
                machine_name=body.machine_name,
                os_name=body.os,
                agent_version=body.agent_version,
                codex_version=body.codex_version,
                device_secret_sha256=body.device_secret_sha256,
                source_ip=ip,
                verify_url=verify_url,
            )
        return result
    except WorkbenchError as exc:
        raise _http(exc) from exc


@public_router.post("/agent-pairing/requests/{pairing_id}/poll")
async def poll_pairing_request(
    pairing_id: str,
    body: DeviceSecret,
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
):
    require_workbench_enabled()
    repo = workbench_store(session)
    try:
        return await _onboarding(repo, cipher).poll(
            pairing_id=pairing_id, device_secret=body.device_secret
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@public_router.post("/agent-pairing/requests/{pairing_id}/cancel")
async def cancel_pairing_request(
    pairing_id: str,
    body: DeviceSecret,
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
):
    require_workbench_enabled()
    repo = workbench_store(session)
    try:
        return await _onboarding(repo, cipher).cancel(
            pairing_id=pairing_id, device_secret=body.device_secret
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@owner_router.post("/workbench/agent-pairing/lookup")
async def lookup_pairing_request(
    body: PairingLookup,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
):
    require_workbench_enabled()
    owner = _actor(principal)
    redis = get_redis().client
    if not await allow_user_lookup(redis, owner):
        raise HTTPException(status_code=429, detail="slow_down")
    repo = workbench_store(session)
    try:
        return await _onboarding(repo, cipher).lookup(
            owner_actor_id=owner, user_code=body.user_code
        )
    except WorkbenchError as exc:
        if exc.http_status == 404 and not await allow_user_lookup_failure(redis, owner):
            raise HTTPException(status_code=429, detail="slow_down") from exc
        raise _http(exc) from exc


@owner_router.post("/workbench/agent-pairing/{pairing_id}/approve")
async def approve_pairing_request(
    pairing_id: str,
    body: PairingApprove,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
    backends=Depends(provisioning_backends),
    issuer=Depends(token_issuer),
):
    require_workbench_enabled()
    owner = _actor(principal)
    redis = get_redis().client
    if not await allow_user_lookup(redis, owner):
        raise HTTPException(status_code=429, detail="slow_down")
    try:
        provision = _provisioning(session, principal, cipher, backends, issuer)
        return await _onboarding(provision.repo, cipher, provision).approve(
            owner_actor_id=owner, pairing_id=pairing_id, user_code=body.user_code
        )
    except WorkbenchError as exc:
        if exc.http_status == 404 and not await allow_user_lookup_failure(redis, owner):
            raise HTTPException(status_code=429, detail="slow_down") from exc
        raise _http(exc) from exc


@owner_router.post("/workbench/agent-pairing/{pairing_id}/deny")
async def deny_pairing_request(
    pairing_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
):
    require_workbench_enabled()
    owner = _actor(principal)
    repo = workbench_store(session)
    try:
        await _onboarding(repo, cipher).deny(
            owner_actor_id=owner, pairing_id=pairing_id
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"status": "denied"}


@owner_router.post("/workbench/agent/install-command")
async def issue_agent_install_command(
    expected: ExpectedInstallRelease | None = None,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    require_workbench_enabled()
    settings = get_settings()
    try:
        release = _download_ready(settings)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    if expected and any(
        getattr(release, field) != value
        for field, value in expected.model_dump().items()
    ):
        raise HTTPException(409, detail="agent_release_changed")
    owner = _actor(principal)
    if not await allow_install_issue(get_redis().client, owner):
        raise HTTPException(status_code=429, detail="slow_down")
    credential = new_install_credential()
    expires = datetime.now(UTC) + INSTALL_TTL
    repo = workbench_store(session)
    async with repo.transaction():
        await repo.create_agent_install_credential(
            owner_actor_id=owner,
            token_hash=install_digest(credential),
            expires_at=expires,
            release_snapshot={
                "release": release.model_dump(mode="json"),
                "template": settings.workbench_agent_install_script_template,
            },
        )
    script_url = (
        settings.web_frontend_base_url.rstrip("/")
        + "/api/agent-install/"
        + credential
        + "/script"
    )
    return {
        "command": f"irm '{script_url}' | iex",
        "expires_at": expires.isoformat(),
    }


@public_router.get(
    "/agent-install/{credential}/script", response_class=PlainTextResponse
)
async def agent_install_script(
    credential: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    require_workbench_enabled()
    settings = get_settings()
    try:
        if request.query_params:
            raise AgentInstallUnavailable("query parameters are not supported")
        release, template = await _issued_release(workbench_store(session), credential)
        package_url = (
            settings.web_frontend_base_url.rstrip("/")
            + "/api/agent-install/"
            + credential
            + "/package"
        )
        values = {
            "PACKAGE_URL": package_url,
            "VERSION": release.version,
            "SIZE_BYTES": str(release.size_bytes),
            "ZIP_SHA256": release.zip_sha256,
            "MANIFEST_SHA256": release.manifest_sha256,
            "CODEX_VERSION": release.codex_version,
        }
        rendered = template
        for name, value in values.items():
            marker = "{{" + name + "}}"
            if rendered.count(marker) != 1:
                raise AgentInstallUnavailable("agent installer template is invalid")
            rendered = rendered.replace(marker, value)
        if re.search(r"\{\{[A-Z_]+\}\}", rendered):
            raise AgentInstallUnavailable("agent installer template is invalid")
        await _consume_install_credential(workbench_store(session), credential)
        return PlainTextResponse(
            rendered,
            media_type="text/plain",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    except WorkbenchError:
        raise HTTPException(
            status_code=404, detail="agent_download_unavailable"
        ) from None


@public_router.get("/agent-install/{credential}/package")
@public_router.head("/agent-install/{credential}/package")
async def agent_install_package(
    credential: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    require_workbench_enabled()
    settings = get_settings()
    if request.query_params:
        raise HTTPException(
            status_code=400, detail="agent_download_parameters_not_supported"
        )
    try:
        release, _ = await _issued_release(workbench_store(session), credential)
    except WorkbenchError:
        raise HTTPException(
            status_code=404, detail="agent_download_unavailable"
        ) from None
    etag = f'"{release.zip_sha256}"'
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "no-store",
        "X-Checksum-Sha256": release.zip_sha256,
        "X-Manifest-Sha256": release.manifest_sha256,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": 'attachment; filename="windows-x64.zip"',
    }
    requested = request.headers.get("range") if request.method == "GET" else None
    if request.headers.get("if-range") not in (None, etag):
        requested = None
    try:
        span = byte_range(requested, release.size_bytes)
    except InvalidRange:
        headers["Content-Range"] = f"bytes */{release.size_bytes}"
        return Response(status_code=416, headers=headers)
    assert release.object_key is not None
    identity = ReleaseIdentity(
        release.object_key,
        release.size_bytes,
        release.zip_sha256,
        release.manifest_sha256,
    )
    from app.bootstrap.workbench import agent_release_store

    if not settings.workbench_agent_release_storage:
        raise HTTPException(503, detail="agent_download_unavailable")
    store = agent_release_store(settings)
    transfer = None
    try:
        object_etag = await run_in_threadpool(store.check, identity)
        length = span[1] - span[0] + 1 if span else release.size_bytes
        headers["Content-Length"] = str(length)
        if span:
            headers["Content-Range"] = f"bytes {span[0]}-{span[1]}/{release.size_bytes}"
        if request.method != "HEAD":
            transfer = await run_in_threadpool(store.open, identity, object_etag, span)
        await _consume_install_credential(workbench_store(session), credential)
        if transfer is None:
            await run_in_threadpool(store.close)
            return Response(headers=headers, media_type="application/zip")
    except WorkbenchError as exc:
        await run_in_threadpool(transfer.close if transfer is not None else store.close)
        raise HTTPException(404, detail="agent_download_unavailable") from exc
    except ReleaseUnavailable as exc:
        await run_in_threadpool(store.close)
        raise HTTPException(
            status_code=503, detail="agent_download_unavailable"
        ) from exc
    except BaseException:
        await run_in_threadpool(transfer.close if transfer is not None else store.close)
        raise
    request_id = getattr(
        request.state, "agent_onboarding_log_id", "request-id-unavailable"
    )

    def safe_chunks():
        try:
            yield from transfer.chunks()
        except Exception:
            # Streaming starts after response headers. The client detects
            # truncation with the configured length and SHA256.
            logger.info(
                "agent_onboarding_result request_id=%s status=stream_error",
                request_id,
            )

    return _PackageResponse(
        safe_chunks(),
        transfer=transfer,
        status_code=206 if span else 200,
        headers=headers,
        media_type="application/zip",
    )
