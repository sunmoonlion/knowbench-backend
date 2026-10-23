"""工作台的网页接口（通道①）。身份从 web 浏览器会话取（OIDC），不信任客户端自报；每次读取重新授权（I3）。

路径前缀 /api/workbench。开关 WORKBENCH_ENABLED。契约版本 1；改字段按 C-C4 双端一起测。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.application.errors.exceptions import AppException
from app.application.workbench.agent_download import (
    InvalidRange,
    ReleaseIdentity,
    ReleaseUnavailable,
    byte_range,
)
from app.application.workbench.agent_service import (
    AgentOffline,
    AgentService,
    AgentServiceUnconfigured,
)
from app.application.workbench.interactions import answer_interaction
from app.application.workbench.interactions import cancel_task as stop_task
from app.application.workbench.ledger import Ledger
from app.application.workbench.provisioning import (
    ProvisioningUnavailable,
    SandboxProvisioning,
)
from app.application.workbench.run_files import task_dossier
from app.application.workbench.session_service import SessionService
from app.application.workbench.tokens import TokenIssuer, exp_of
from app.bootstrap.workbench import (
    agent_release_store,
    build_agent_service,
    build_provisioner,
    build_relay_admin,
    workbench_store,
)
from app.domain.security import Principal
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.provider_url import (
    normalize_mcp_sources,
    validate_provider_base_url,
)
from app.infrastructure.storage.postgres import get_db_session, get_postgres
from app.infrastructure.storage.redis import get_redis
from app.interfaces.http.middleware.auth import get_web_current_user
from core.config import get_settings

# 2：项目、对话的种类（PRD/apps/investment.md）。旧的建会话入参仍然接受。
CONTRACT_VERSION = 2


def require_workbench_enabled() -> None:
    if not get_settings().workbench_enabled:
        raise HTTPException(status_code=404, detail="workbench is disabled")


router = APIRouter(
    prefix="/workbench",
    tags=["Workbench"],
    dependencies=[Depends(require_workbench_enabled)],
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RotateRelayIdentity(Strict):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class RegisterEnvironment(Strict):
    name: str = Field(min_length=1, max_length=128)
    agent_version: str | None = None
    codex_version: str | None = None
    roots: list[str] = Field(default_factory=list)
    ceiling: dict[str, Any] = Field(
        default_factory=lambda: {"sandbox": "workspace-write", "network": False}
    )


class RegisterSandbox(Strict):
    app_server_url: str = Field(pattern=r"^wss?://")
    token_ref: str = Field(pattern=r"^(env|file|inline):")
    codex_version: str | None = None


class CreateSession(Strict):
    """两种写法。新的：`kind`（聊天可不给 `project_id`）。旧的：机器、沙箱、完整目录。"""

    kind: str | None = Field(default=None, pattern=r"^(chat|work|expert)$")
    project_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=400)
    environment_id: str | None = None
    sandbox_id: str | None = None
    project_root: str | None = Field(default=None, min_length=1)
    thread_settings: dict[str, Any] | None = None


class StartTurn(Strict):
    text: str = Field(min_length=1, max_length=20000)
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=128)


class Interrupt(Strict):
    turn_id: str | None = None


class Handover(Strict):
    idempotency_key: str = Field(min_length=8, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    profile_version: str | None = None
    original_input: dict[str, Any]
    attachments: list[str] = Field(default_factory=list)
    # 上限可以不给：不给就没有上限。页面上实时显示花了多少，用户随时可以停
    budget_limit: Decimal | None = Field(default=None, gt=0)
    budget_currency: str = "CNY"
    requested_deadline: str | None = None
    client_context: dict[str, Any] = Field(default_factory=dict)


class RespondInteraction(Strict):
    token: str = Field(min_length=16)
    decision: str | None = None
    answer: dict[str, Any] | None = None
    amount: str | None = None
    subject_digest: str | None = None


class Prefs(Strict):
    model: str | None = Field(default=None, max_length=128)
    approval_policy: str = Field(
        default="on-request", pattern=r"^(untrusted|on-request|on-failure|never)$"
    )
    # Codex 在云端能用哪些数据源（0021 §二之 2）；不给 = 不改/全开
    mcp_sources: dict[str, bool] | None = None


class AddCredential(Strict):
    provider: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=8, max_length=512)
    sandbox_id: str | None = None
    # 自建或代理的 OpenAI 兼容端点（0021 §二之 2）；空 = 厂商默认
    base_url: str | None = Field(default=None, max_length=512)


def credential_cipher():
    """BYOK 凭据的对称加密（Fernet）；未配置密钥即 503。测试用依赖覆盖注入。"""
    key = get_settings().workbench_credential_key
    if not key:
        raise AppException(
            code="credential_store_unconfigured",
            status_code=503,
            msg="credential store is not configured",
        )
    from cryptography.fernet import Fernet

    return Fernet(key.encode())


def provisioning_backends():
    """供给器与会合点管理通道的客户端；未配置即 503。测试用依赖覆盖注入假的。"""
    s = get_settings()
    if not (
        s.workbench_provisioner_url
        and s.workbench_provisioner_token
        and s.workbench_relay_admin_url
        and s.workbench_relay_admin_token
    ):
        raise _http(ProvisioningUnavailable("sandbox provisioning is not configured"))
    provisioner = build_provisioner(
        url=s.workbench_provisioner_url,
        token=s.workbench_provisioner_token,
        model_provider=s.workbench_sandbox_model_provider,
        model=s.workbench_sandbox_model,
        provider_base_url=s.workbench_sandbox_provider_base_url,
    )
    admin = build_relay_admin(
        s.workbench_relay_admin_url, s.workbench_relay_admin_token
    )
    return provisioner, admin, s.workbench_relay_public_url


def sandbox_global_limit() -> int:
    """F-SBX-08 的上限；测试用依赖覆盖。"""
    return get_settings().workbench_sandbox_global_limit


def token_issuer() -> TokenIssuer | None:
    """D10：配置了签名私钥才签 JWT；否则供给走不透明随机令牌。"""
    s = get_settings()
    if not s.workbench_token_signing_key:
        return None
    return TokenIssuer(s.workbench_token_signing_key, issuer=s.workbench_token_issuer)


def agent_service() -> AgentService:
    """0022：工作台到代理的服务流。要会合点地址（与管理通道同一个根）和签名私钥；没配即 503。测试用依赖覆盖注入假的。"""
    s = get_settings()
    if not (s.workbench_relay_admin_url and s.workbench_token_signing_key):
        raise _http(AgentServiceUnconfigured("agent service channel is not configured"))
    return build_agent_service(
        s.workbench_relay_admin_url,
        TokenIssuer(s.workbench_token_signing_key, issuer=s.workbench_token_issuer),
    )


def optional_agent_service() -> AgentService | None:
    """同上，但没配就给 None：用在「问得到就核一下，问不到照做」的地方（新建项目核目录，0024）。"""
    s = get_settings()
    if not (s.workbench_relay_admin_url and s.workbench_token_signing_key):
        return None
    return build_agent_service(
        s.workbench_relay_admin_url,
        TokenIssuer(s.workbench_token_signing_key, issuer=s.workbench_token_issuer),
    )


def _actor(principal: Principal) -> str:
    if principal.actor_id is None:
        raise HTTPException(status_code=403, detail="principal has no actor id")
    return str(principal.actor_id)


# 不给浏览器的列。token_ref 是沙箱的令牌在哪（按需拉起的沙箱，它就是令牌本身）
_NEVER_TO_THE_BROWSER = frozenset({"token_hash", "token_ref"})


def _plain(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if k in _NEVER_TO_THE_BROWSER:
            continue
        out[k] = str(v) if isinstance(v, uuid.UUID) else v
    return out


def _http(exc: WorkbenchError) -> AppException:
    # 走统一的 problem+json：顶层 code / status / detail
    return AppException(code=exc.code, status_code=exc.http_status, msg=exc.message)


async def _publish_commands_wakeup() -> None:
    try:
        await get_redis().client.publish(
            f"{get_settings().workbench_redis_key_prefix}:commands", "1"
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------- environments / sandboxes ----------------
@router.get("/environments")
async def list_environments(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    return {
        "contract_version": CONTRACT_VERSION,
        "environments": [
            _plain(e)
            for e in await repo.list_environments(owner_actor_id=_actor(principal))
        ],
    }


@router.get("/local-link")
async def local_link(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
):
    """打开工作台时的核对（会合点备忘第五节，账 143）：配给当前账号的那份本地 link 在哪台机器上（带设备编号乙）、
    它连没连着；配置像不像是从别的电脑拷来的（第一次登记时的主机名和现在的对不上）；令牌哪天到期。
    网页拿乙和浏览器所在电脑上本地 link 自己报的编号（甲）比，不在这里判。"""
    owner = _actor(principal)
    repo = workbench_store(session)
    env = await repo.assigned_environment(owner)
    identity = await repo.get_relay_identity(owner)
    expires_at = None
    if identity and identity.get("agent_token_ciphertext"):
        try:
            token = cipher.decrypt(identity["agent_token_ciphertext"].encode()).decode()
        except Exception:  # noqa: BLE001  密文对不上就当不知道，不挡页面
            token = ""
        exp = exp_of(token) if token else None
        expires_at = datetime.fromtimestamp(exp, UTC).isoformat() if exp else None
    copied = bool(
        env
        and env.get("first_host")
        and env.get("host")
        and env["first_host"] != env["host"]
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "assigned": _plain(env) if env else None,
        "copied": copied,
        "token_expires_at": expires_at,
    }


@router.post("/environments", status_code=201)
async def register_environment(
    body: RegisterEnvironment,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    async with repo.transaction():
        env_id = await repo.register_environment(
            owner_actor_id=_actor(principal),
            name=body.name,
            agent_version=body.agent_version,
            codex_version=body.codex_version,
            roots=body.roots,
            ceiling=body.ceiling,
        )
    return {"environment_id": env_id}


# ---------------- 代理（0022，账 87）：服务流给云端自用；本机访问票给网页敲回环口 ----------------
async def _relay_user_of(repo, actor: str, environment_id: str) -> str:
    """这台电脑是不是他的、他有没有配对过的会合点身份。都对才知道该连谁。"""
    await repo.get_environment(environment_id, owner_actor_id=actor)
    identity = await repo.get_relay_identity(actor)
    if not identity or not identity.get("relay_user"):
        raise AgentOffline("no agent has been paired for this account")
    return str(identity["relay_user"])


async def _ask_agent(
    service: AgentService,
    session: AsyncSession,
    principal: Principal,
    environment_id: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repo = workbench_store(session)
    try:
        relay_user = await _relay_user_of(repo, _actor(principal), environment_id)
        return await service.call(relay_user, method, params)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/environments/{environment_id}/service")
async def agent_service_hello(
    environment_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: AgentService = Depends(agent_service),
):
    """经会合点服务流问代理（云端自用：成稿硬检查等）。网页看文件树不走这里，走回环口（0022 乙案）。"""
    return await _ask_agent(
        service, session, principal, environment_id, "service/hello"
    )


@router.get("/environments/{environment_id}/local-ticket")
async def local_agent_ticket(
    environment_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    issuer: TokenIssuer | None = Depends(token_issuer),
):
    """0022 乙案：网页要一张票，拿去敲那台电脑上代理的回环口（127.0.0.1:47811）。
    文件内容不经工作台；这里只核这台电脑是不是他的、他配对过没有，然后签 10 分钟的票。"""
    if issuer is None:
        raise _http(AgentServiceUnconfigured("ticket signing is not configured"))
    repo = workbench_store(session)
    try:
        relay_user = await _relay_user_of(repo, _actor(principal), environment_id)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    issued = issuer.local_agent_ticket(relay_user)
    return {
        "ticket": issued.token,
        "expires_at": issued.expires_at,
        "relay_user": relay_user,
    }


@router.get("/sandboxes")
async def list_sandboxes(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    return {
        "contract_version": CONTRACT_VERSION,
        "sandboxes": [
            _plain(x)
            for x in await repo.list_sandboxes(owner_actor_id=_actor(principal))
        ],
    }


@router.post("/sandboxes", status_code=201)
async def register_sandbox(
    body: RegisterSandbox,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    async with repo.transaction():
        sb_id = await repo.register_sandbox(
            owner_actor_id=_actor(principal),
            app_server_url=body.app_server_url,
            token_ref=body.token_ref,
            codex_version=body.codex_version,
        )
    return {"sandbox_id": sb_id}


# ---------------- sessions ----------------
@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSession,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    if body.kind is not None:
        if (
            body.environment_id
            or body.sandbox_id
            or body.project_root
            or body.thread_settings is not None
        ):
            # 沙箱由后端取；权限由后端按种类定，页面不能传（F-PROJ-03）
            raise HTTPException(
                status_code=422,
                detail="with kind, only project_id and title may be given",
            )
        try:
            result = await SessionService(repo).start(
                owner_actor_id=_actor(principal),
                kind=body.kind,
                project_id=body.project_id,
                title=body.title,
            )
            created = await repo.get_session(result["session_id"])
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=result["session_id"],
                    sandbox_id=str(created["sandbox_id"]),
                    kind="session.start_thread",
                    payload={},
                )
        except WorkbenchError as exc:
            raise _http(exc) from exc
        await _publish_commands_wakeup()
        return result
    if not (body.environment_id and body.sandbox_id and body.project_root):
        raise HTTPException(
            status_code=422,
            detail="environment_id, sandbox_id and project_root are required",
        )
    try:
        thread_settings = body.thread_settings
        if thread_settings is None:  # 用户设置面的偏好进 thread 设置（0002「设置」）
            prefs = await repo.get_prefs(_actor(principal))
            thread_settings = {
                "approvalPolicy": prefs["approval_policy"],
                "sandbox": "workspace-write",
            }
            if prefs["model"]:
                thread_settings["model"] = prefs["model"]
        result = await SessionService(repo).create(
            owner_actor_id=_actor(principal),
            environment_id=body.environment_id,
            sandbox_id=body.sandbox_id,
            project_root=body.project_root,
            thread_settings=thread_settings,
        )
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=result["session_id"],
                sandbox_id=body.sandbox_id,
                kind="session.start_thread",
                payload={},
            )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await _publish_commands_wakeup()
    return result


@router.get("/sessions")
async def list_sessions(
    project_id: str | None = Query(default=None, max_length=64),
    without_project: bool = Query(default=False),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """我的对话。可只要某个项目里的，或只要不属于任何项目的聊天。"""
    repo = workbench_store(session)
    if project_id is not None:
        try:
            await repo.get_project(project_id, owner_actor_id=_actor(principal))
        except WorkbenchError as exc:
            raise _http(exc) from exc
    return {
        "contract_version": CONTRACT_VERSION,
        "sessions": [
            _plain(x)
            for x in await repo.list_sessions(
                owner_actor_id=_actor(principal),
                project_id=project_id,
                without_project=without_project,
            )
        ],
    }


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await SessionService(workbench_store(session)).view(
            session_id, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post("/sessions/{session_id}/turns", status_code=202)
async def start_turn(
    session_id: str,
    body: StartTurn,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    try:
        event = await SessionService(repo).record_user_turn_requested(
            session_id,
            owner_actor_id=_actor(principal),
            text=body.text,
            request_id=body.request_id,
        )
        s = await repo.get_session(session_id)
        async with repo.transaction():
            cid = await repo.enqueue_command(
                session_id=session_id,
                sandbox_id=str(s["sandbox_id"]),
                kind="turn.start",
                payload={
                    "text": body.text,
                    "request_id": body.request_id,
                    "by": "user",
                },
            )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await _publish_commands_wakeup()
    return {"request_id": body.request_id, "command_id": cid, "cursor": event["cursor"]}


@router.post("/sessions/{session_id}/interrupt", status_code=202)
async def interrupt(
    session_id: str,
    body: Interrupt,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    try:
        s = await SessionService(repo).assert_user_may_drive(
            session_id, owner_actor_id=_actor(principal)
        )
        async with repo.transaction():
            cid = await repo.enqueue_command(
                session_id=session_id,
                sandbox_id=str(s["sandbox_id"]),
                kind="turn.interrupt",
                payload={"turn_id": body.turn_id},
            )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await _publish_commands_wakeup()
    return {"command_id": cid}


@router.get("/sessions/{session_id}/usage")
async def session_usage(
    session_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """这段对话到现在用了多少、花了多少（估算）。实时的数在事件流里，这里是合计。"""
    try:
        usage = await SessionService(workbench_store(session)).usage(
            session_id,
            owner_actor_id=_actor(principal),
            prices=get_settings().workbench_model_prices(),
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **usage}


@router.get("/sessions/{session_id}/events")
async def list_events(
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    try:
        await repo.get_session(session_id, owner_actor_id=_actor(principal))
    except WorkbenchError as exc:
        raise _http(exc) from exc
    events = await repo.list_events(
        session_id=session_id, after_cursor=after, limit=limit
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "events": events,
        "next_cursor": events[-1]["cursor"] if events else after,
    }


def sse_frame(ev: dict[str, Any]) -> str:
    """一帧 SSE。不带 `event:` 名：浏览器 EventSource 只把无名（或名为 message）的帧交给 onmessage，
    带了事件名（如 turn/completed）的帧会被静默丢掉。事件类型在 data 里的 `type` 字段。"""
    return f"id: {ev.get('cursor')}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"


async def stream_session_events(
    *,
    fetch_after: Callable[[int], Awaitable[list[dict[str, Any]]]],
    wait_wakeup: Callable[[float], Awaitable[bool]],
    is_disconnected: Callable[[], Awaitable[bool]],
    after: int = 0,
    poll_seconds: float = 2.0,
    keepalive_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """事件流的核心循环。数据库是唯一的顺序来源：Redis 消息只当"有新事件"的唤醒信号，醒了（或每 poll_seconds）
    就按 cursor 取 last 之后的全部事件依次发出——所以只写库、没发 Redis 的事件（任务状态、顾问步骤、用户发言）
    也能实时到，而且不会因为先收到后面的事件而把前面没发布的跳过（KIND 09 实测：委托状态要刷新才出现）。
    空闲 keepalive_seconds 发一行 SSE 注释保活（EventSource 忽略注释）。"""
    last = after
    idle = 0.0
    while True:
        events = await fetch_after(last)
        for ev in events:
            last = max(last, int(ev["cursor"]))
            yield sse_frame(ev)
        if events:
            idle = 0.0
        if await is_disconnected():
            return
        woke = await wait_wakeup(poll_seconds)
        if not woke:
            idle += poll_seconds
            if idle >= keepalive_seconds:
                idle = 0.0
                yield ": keepalive\n\n"


@router.get("/sessions/{session_id}/stream")
async def stream_events(
    session_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """SSE：先订阅 Redis（只作唤醒），再按 cursor 从数据库取事件；见 stream_session_events。"""
    repo = workbench_store(session)
    try:
        await repo.get_session(session_id, owner_actor_id=_actor(principal))
    except WorkbenchError as exc:
        raise _http(exc) from exc
    channel = f"{get_settings().workbench_redis_key_prefix}:session:{session_id}:events"

    async def gen():
        pubsub = get_redis().client.pubsub()
        await pubsub.subscribe(channel)

        async def fetch_after(cursor: int) -> list[dict[str, Any]]:
            async with get_postgres().session_factory() as s2:
                return await workbench_store(s2).list_events(
                    session_id=session_id, after_cursor=cursor, limit=1000
                )

        async def wait_wakeup(wait_seconds: float) -> bool:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=wait_seconds
            )
            return message is not None

        try:
            async for frame in stream_session_events(
                fetch_after=fetch_after,
                wait_wakeup=wait_wakeup,
                is_disconnected=request.is_disconnected,
                after=after,
            ):
                yield frame
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------- handover / tasks ----------------
@router.post("/sessions/{session_id}/handover", status_code=201)
async def handover(
    session_id: str,
    body: Handover,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    req = HandoverRequest(session_id=session_id, **body.model_dump())
    try:
        result = await Ledger(repo).handover(req, owner_actor_id=_actor(principal))
        if result["created"] and not result.get("queued_behind"):
            s = await repo.get_session(session_id)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=session_id,
                    sandbox_id=str(s["sandbox_id"]),
                    kind="task.drive",
                    payload={"task_id": result["task_id"]},
                )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await _publish_commands_wakeup()
    return result


@router.get("/tasks")
async def list_tasks(
    session_id: str | None = None,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    return {
        "contract_version": CONTRACT_VERSION,
        "tasks": [
            {**_plain(t), "dossier": task_dossier(t)}
            for t in await repo.list_tasks(
                owner_actor_id=_actor(principal), session_id=session_id
            )
        ],
    }


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await Ledger(workbench_store(session)).task_view(
            task_id, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(
    task_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    try:
        # 专家正在做某一步（在跑，或卡在等你批准、等你回答）：马上把这一轮停下，不等它自己做完
        result = await stop_task(repo, task_id, owner_actor_id=_actor(principal))
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await _publish_commands_wakeup()
    return result


@router.get("/tasks/{task_id}/artifacts")
async def task_artifacts(
    task_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """底稿页：每个交回物的最新版本带内容。不渲染任何评级、目标价字段（F-WEB-05 由网页守，后端只给事实）。"""
    repo = workbench_store(session)
    try:
        await repo.get_task(task_id, owner_actor_id=_actor(principal))
    except WorkbenchError as exc:
        raise _http(exc) from exc
    arts = await repo.list_artifacts_with_content(task_id)
    return {
        "contract_version": CONTRACT_VERSION,
        "artifacts": [_plain(a) for a in arts],
    }


# ---------------- sandbox provisioning (0003 D9) ----------------
def _provisioning(
    session: AsyncSession,
    principal: Principal,
    cipher,
    backends,
    issuer=None,
    global_limit: int = 0,
) -> SandboxProvisioning:
    provisioner, relay_admin, relay_public_url = backends
    return SandboxProvisioning(
        workbench_store(session),
        cipher=cipher,
        provisioner=provisioner,
        relay_admin=relay_admin,
        relay_public_url=relay_public_url,
        issuer=issuer,
        global_limit=global_limit,
        records_url=records_mcp_url(),
    )


def records_mcp_url() -> str:
    """沙箱里要不要配读项目记录的工具服务：开关开着、地址也配了才配。"""
    s = get_settings()
    return s.workbench_records_mcp_url if s.workbench_records_mcp_enabled else ""


@router.get("/token-keys")
async def token_keys(issuer: TokenIssuer | None = Depends(token_issuer)):
    """公钥集（JWKS）：边缘与知识服务用它就地验工作台签发的令牌（D10）。未配签名密钥时为空。"""
    if issuer is None:
        return {"keys": []}
    return issuer.public_jwks()


@router.get("/agent/download")
async def agent_download(principal: Principal = Depends(get_web_current_user)):
    descriptor = get_settings().workbench_agent_download
    return {
        "contract_version": CONTRACT_VERSION,
        "download": descriptor.public_descriptor() if descriptor else None,
    }


@router.api_route("/agent/package", methods=["GET", "HEAD"])
def agent_package(
    request: Request, principal: Principal = Depends(get_web_current_user)
):
    settings = get_settings()
    release = settings.workbench_agent_download
    if not release or release.mode != "object-storage":
        raise HTTPException(404, "agent_download_unavailable")
    if request.query_params:
        raise HTTPException(400, "agent_download_parameters_not_supported")
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
        return Response(
            status_code=416,
            headers={
                **headers,
                "Content-Range": f"bytes */{release.size_bytes}",
            },
        )
    identity = ReleaseIdentity(
        release.object_key,
        release.size_bytes,
        release.zip_sha256,
        release.manifest_sha256,
    )
    store = agent_release_store(settings)
    try:
        object_etag = store.check(identity)
        length = span[1] - span[0] + 1 if span else release.size_bytes
        headers["Content-Length"] = str(length)
        if span:
            headers["Content-Range"] = f"bytes {span[0]}-{span[1]}/{release.size_bytes}"
        if request.method == "HEAD":
            store.close()
            return Response(headers=headers, media_type="application/zip")
        transfer = store.open(identity, object_etag, span)
    except ReleaseUnavailable as exc:
        store.close()
        raise HTTPException(503, "agent_download_unavailable") from exc
    except Exception:
        store.close()
        raise
    return _PackageResponse(
        transfer.chunks(),
        transfer=transfer,
        status_code=206 if span else 200,
        headers=headers,
        media_type="application/zip",
    )


class _PackageResponse(StreamingResponse):
    """Close upstream even when the client disconnects before consuming the iterator."""

    def __init__(self, *args, transfer, **kwargs):
        super().__init__(*args, **kwargs)
        self.transfer = transfer

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.transfer.close)


@router.post("/sandboxes/provision")
async def provision_sandbox(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
    backends=Depends(provisioning_backends),
    issuer: TokenIssuer | None = Depends(token_issuer),
    global_limit: int = Depends(sandbox_global_limit),
):
    """用设置页登记的 key 给这个用户拉起（或更新）他的沙箱。首次同时签发会合点身份，代理令牌只在这次响应里。
    全局在跑沙箱数到上限时，新用户得到 503 sandbox_capacity_full（F-SBX-08）。"""
    try:
        result = await _provisioning(
            session, principal, cipher, backends, issuer, global_limit
        ).provision(_actor(principal))
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **result}


@router.post("/sandboxes/relay-identity/rotate")
async def rotate_relay_identity(
    body: RotateRelayIdentity,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
    backends=Depends(provisioning_backends),
    issuer: TokenIssuer | None = Depends(token_issuer),
):
    """撤换会合点令牌（D10）：旧的按 jti 吊销并推到会合点，新代理令牌只在这次响应里。沙箱在线则滚动。"""
    try:
        result = await _provisioning(
            session, principal, cipher, backends, issuer
        ).rotate_relay_identity(_actor(principal), body.expected_revision)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return result


@router.get("/sandboxes/provisioned")
async def provisioned_sandbox_status(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
    backends=Depends(provisioning_backends),
):
    try:
        result = await _provisioning(session, principal, cipher, backends).status(
            _actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **result}


@router.delete("/sandboxes/provisioned")
async def deprovision_sandbox(
    purge: bool = Query(default=False),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
    backends=Depends(provisioning_backends),
):
    try:
        result = await _provisioning(session, principal, cipher, backends).deprovision(
            _actor(principal), purge=purge
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **result}


# ---------------- settings: prefs & credentials ----------------
@router.get("/prefs")
async def get_prefs(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    prefs = await workbench_store(session).get_prefs(_actor(principal))
    return {"contract_version": CONTRACT_VERSION, **_plain(_with_sources(prefs))}


def _with_sources(prefs: dict[str, Any]) -> dict[str, Any]:
    return {**prefs, "mcp_sources": normalize_mcp_sources(prefs.get("mcp_sources"))}


@router.put("/prefs")
async def put_prefs(
    body: Prefs,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    try:
        sources = (
            normalize_mcp_sources(body.mcp_sources)
            if body.mcp_sources is not None
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    async with repo.transaction():
        if sources is None:
            current = await repo.get_prefs(_actor(principal))
            sources = current.get("mcp_sources")
        prefs = await repo.put_prefs(
            _actor(principal),
            model=body.model,
            approval_policy=body.approval_policy,
            mcp_sources=sources,
        )
    return {"contract_version": CONTRACT_VERSION, **_plain(_with_sources(prefs))}


class MemoryIn(Strict):
    text: str = Field(min_length=1, max_length=400)
    kind: str = Field(default="fact", pattern=r"^(fact|judgement)$")
    source: dict[str, Any] | None = None


class MemoryPatch(Strict):
    text: str | None = Field(default=None, min_length=1, max_length=400)
    enabled: bool | None = None


# ---- 长期记忆：关于这位用户的几条事实，每段对话开头带上（0019 §四，账 85）----
@router.get("/memories")
async def list_memories(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await workbench_store(session).list_memories(_actor(principal))
    return {"contract_version": CONTRACT_VERSION, "memories": [_plain(r) for r in rows]}


@router.post("/memories", status_code=201)
async def add_memory(
    body: MemoryIn,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    async with repo.transaction():
        made = await repo.insert_memory(
            owner_actor_id=_actor(principal),
            kind=body.kind,
            text=body.text.strip(),
            source=body.source,
        )
    return {"contract_version": CONTRACT_VERSION, "memory": _plain(made)}


@router.patch("/memories/{memory_id}")
async def change_memory(
    memory_id: str,
    body: MemoryPatch,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    async with repo.transaction():
        changed = await repo.update_memory(
            memory_id,
            owner_actor_id=_actor(principal),
            text=body.text.strip() if body.text is not None else None,
            enabled=body.enabled,
        )
    if changed is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return {"contract_version": CONTRACT_VERSION, "memory": _plain(changed)}


@router.delete("/memories/{memory_id}", status_code=204)
async def remove_memory(
    memory_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    async with repo.transaction():
        gone = await repo.delete_memory(memory_id, owner_actor_id=_actor(principal))
    if not gone:
        raise HTTPException(status_code=404, detail="memory not found")
    return Response(status_code=204)


@router.delete("/memories", status_code=200)
async def clear_memories(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    async with repo.transaction():
        n = await repo.clear_memories(_actor(principal))
    return {"contract_version": CONTRACT_VERSION, "removed": n}


@router.get("/credentials")
async def list_credentials(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await workbench_store(session).list_credentials(_actor(principal))
    return {
        "contract_version": CONTRACT_VERSION,
        "credentials": [_plain(r) for r in rows],
    }


@router.post("/credentials", status_code=201)
async def add_credential(
    body: AddCredential,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    cipher=Depends(credential_cipher),
):
    """key 只经 HTTPS 提交一次，服务端存密文，接口永不回显（F-WEB-01、C-D9）。"""
    repo = workbench_store(session)
    if body.sandbox_id:
        try:
            await repo.get_sandbox(body.sandbox_id, owner_actor_id=_actor(principal))
        except WorkbenchError as exc:
            raise _http(exc) from exc
    base_url = None
    if body.base_url is not None and body.base_url.strip():
        try:
            base_url = validate_provider_base_url(body.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    ciphertext = cipher.encrypt(body.api_key.encode()).decode()
    async with repo.transaction():
        row = await repo.add_credential(
            owner_actor_id=_actor(principal),
            sandbox_id=body.sandbox_id,
            provider=body.provider,
            ciphertext=ciphertext,
            hint=body.api_key[-4:],
            base_url=base_url,
        )
    return _plain(row)


@router.post("/credentials/{credential_id}/revoke")
async def revoke_credential(
    credential_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    async with repo.transaction():
        ok = await repo.revoke_credential(
            credential_id, owner_actor_id=_actor(principal)
        )
    if not ok:
        raise AppException(
            code="credential_not_found", status_code=404, msg="no active credential"
        )
    return {"credential_id": credential_id, "status": "revoked"}


# ---------------- interactions ----------------
@router.post("/interactions/{interaction_id}/respond")
async def respond_interaction(
    interaction_id: str,
    body: RespondInteraction,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    response: dict[str, Any] = {
        k: v
        for k, v in {
            "decision": body.decision,
            "answer": body.answer,
            "amount": body.amount,
        }.items()
        if v is not None
    }
    try:
        # 消费交互，并排上接着驾驶 / 把答复送到 runner 的命令（与跑分身测试共用）
        result = await answer_interaction(
            repo,
            interaction_id,
            token=body.token,
            response=response,
            owner_actor_id=_actor(principal),
            subject_digest=body.subject_digest,
            decision=body.decision,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await _publish_commands_wakeup()
    return result
