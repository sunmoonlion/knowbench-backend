"""专家：有哪些专家、请专家、委托的步骤、底稿、等我决定的事（PRD/apps/investment-expert.md）。

方法的原文不出后端：这里的任何接口都不返回它。专家有两种来源：内置专家包，和用户从 knowmart
请来、在这里核验钉住的自己的分身（SDD 0027 P4，id 写作 `knowbot:<分身>@<版本>`）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench import (
    expert_duties,
    handoff_adoption,
    planning_probe,
    temporary_planning,
)
from app.application.workbench.catalog import (
    my_knowbots,
    pin_knowbot,
    viewable_expert,
)
from app.application.workbench.delegation_receipts import (
    HandoffClosure,
    handoff_evidence,
    handoffs,
)
from app.application.workbench.expert_data import refusal
from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.formal_expert_preparation import (
    ExecutionConfirmation,
    Preparation,
    prepare,
)
from app.application.workbench.formal_handoff import Handoff, transfer
from app.application.workbench.ordinary_partial_delivery import (
    BranchReceipt,
    partial_delivery,
)
from app.application.workbench.temporary_planning import Adoption, PlanningRequest
from app.bootstrap.workbench import (
    delegation_service,
    dynamic_market_owners,
    expert_data,
    find_expert_service,
    knowbase_v2,
    knowmart_summoner,
)
from app.bootstrap.workbench import (
    web_workbench_store as workbench_store,
)
from app.domain.security import Principal
from app.domain.workbench.errors import (
    StaleStateVersion,
    UserDataUnavailable,
    WorkbenchError,
)
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.pack_view import pack_view
from app.domain.workbench.packs import listed_packs
from app.domain.workbench.supply_prematch import eta_of
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.commands_wakeup import publish_commands_wakeup
from app.interfaces.endpoints.workbench_routes import (
    CONTRACT_VERSION,
    Pick,
    _actor,
    _http,
    require_workbench_enabled,
)
from app.interfaces.http.middleware.auth import (
    get_web_current_user,
    get_web_knowbase_token_provider,
)
from core.config import get_settings

router = APIRouter(
    prefix="/workbench",
    tags=["Workbench"],
    dependencies=[Depends(require_workbench_enabled)],
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Delegate(Strict):
    """请专家。专家看得到什么、能做什么由后端按项目定，这里不收任何设置。"""

    idempotency_key: str = Field(min_length=8, max_length=128)
    expert: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    # 上限可以不给：不给就没有上限。页面上实时显示花了多少，用户随时可以停
    budget_limit: Decimal | None = Field(default=None, gt=0, le=Decimal("100000"))
    budget_currency: str = Field(default="CNY", pattern=r"^CNY$")


class ConfirmData(Strict):
    """用户确认专家提出的数据（第 0 步或运行中，decisions ⑭）：可以先改（`needs` 是
    改过的整份：换主体、改期间、去掉、自己加一样），换一份（条目编号 → 资料与版本）、
    去掉几样（条目编号）。都不给就是照提出的确认。"""

    view_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    picks: dict[str, Pick] = Field(default_factory=dict, max_length=200)
    dropped: list[str] = Field(default_factory=list, max_length=200)
    needs: list[dict[str, Any]] | None = Field(default=None, max_length=20)


class PinKnowbot(Strict):
    """从 knowmart「我的分身」选定回来：`<分身>@<版本>`。"""

    knowbot: str = Field(min_length=3, max_length=64)


def _view(pack: Any, source: str) -> dict[str, Any]:
    return {**pack_view(pack), "source": source}


@router.get("/packs")
async def packs(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """有哪些专家：名字、解决什么、不解决什么、每一步的名字与一句白话、不通过时怎么办。

    内置的在前，然后是这个人请过的分身（只有他自己的）。
    """
    store = workbench_store(session)
    mine = await my_knowbots(store, owner_actor_id=_actor(principal))
    hidden = await store.hidden_experts(owner_actor_id=_actor(principal))
    return {
        "contract_version": CONTRACT_VERSION,
        # 默认预算与币种所有者还没定（W4）：先不给默认值，页面让用户自己填
        "budget": {"currency": "CNY", "default": None},
        "packs": [
            _view(p, "builtin") for p in listed_packs() if p.profile_id not in hidden
        ]
        + [_view(p, "knowbot") for p in mine if p.profile_id not in hidden],
    }


@router.get("/packs/{pack_id}")
async def one_pack(
    pack_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        pack = await viewable_expert(
            workbench_store(session),
            owner_actor_id=_actor(principal),
            profile_id=pack_id,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    source = "knowbot" if pack_id.startswith("knowbot:") else "builtin"
    return {"contract_version": CONTRACT_VERSION, "pack": _view(pack, source)}


@router.delete("/packs/{pack_id}")
async def remove_expert(
    pack_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """只移除本人列表项；运行中/历史任务的快照与读取保持可用。再次请回即恢复。"""
    store = workbench_store(session)
    try:
        await viewable_expert(
            store, owner_actor_id=_actor(principal), profile_id=pack_id
        )
        async with store.transaction():
            await store.hide_expert(
                owner_actor_id=_actor(principal), profile_id=pack_id
            )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION}


@router.get("/packs/{pack_id}/data-needs")
async def data_needs(
    pack_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """请这位专家用什么数据：分身回 `contract`（knowbot-manifest/v2）与作者挑选的数据的
    份数 `bound`；要什么数据由委托第 0 步按问题提出、用户确认，请专家页上不用选
    （decisions ⑭）。内置专家不用数据：`contract` 是空的。
    """
    try:
        found = await delegation_service(session).data_needs(
            owner_actor_id=_actor(principal), profile_id=pack_id
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **found}


@router.post("/knowbots/pin")
async def pin(
    body: PinKnowbot,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """用户在 knowmart 选定、跳回来：按他的会话向 knowmart 核一次再钉住。

    所有者只取自会话（Casdoor 用户 id），链接里带来的只有 `<分身>@<版本>`；伪造的、别人的都是 404。
    """
    try:
        pack = await pin_knowbot(
            workbench_store(session),
            knowmart_summoner(),
            owner_actor_id=_actor(principal),
            owner_subject=principal.subject,
            ref=body.knowbot,
            dynamic_method_owners=dynamic_market_owners(),
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, "pack": _view(pack, "knowbot")}


@router.get("/tasks/{task_id}/steps")
async def task_steps(
    task_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """一个委托做到哪了：委托单、步骤轨。每一步的交回物与过程另取（下一个接口）。"""
    try:
        view = await ExpertDesk(workbench_store(session)).steps(
            task_id, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **view}


@router.get("/tasks/{task_id}/steps/{number}")
async def task_step(
    task_id: str,
    number: int = Path(ge=1, le=100),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """一步的全部：每一次做的交回物、逐条的验收结果、过程。"""
    try:
        view = await ExpertDesk(workbench_store(session)).step(
            task_id, number, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **view}


@router.post("/projects/{project_id}/delegations", status_code=201)
async def delegate(
    project_id: str,
    body: Delegate,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    user_token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
) -> dict[str, Any]:
    """从专家入口请专家：新建一段工作对话并立刻交给专家。要么都成，要么什么都不留下。"""
    question = body.question.strip()
    try:
        if not question:
            raise WorkbenchError("the question is empty")
        made = await delegation_service(session).from_expert_entry(
            owner_actor_id=_actor(principal),
            project_id=project_id,
            profile_id=body.expert,
            question=question,
            budget_limit=body.budget_limit,
            budget_currency=body.budget_currency,
            idempotency_key=body.idempotency_key,
            user_token=user_token,
            expert_data=expert_data(),
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    if made["created"]:
        await publish_commands_wakeup()
    return {"contract_version": CONTRACT_VERSION, **made}


@router.get("/tasks/{task_id}/data")
async def task_data(
    task_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    user_token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
) -> dict[str, Any]:
    """这件委托用的数据（分身）：在定数据、钉了哪几份、在等哪几项、专家提出了什么（每一样
    解析到哪、候选有哪些；要去取的带 knowfeed 预切合的结果 `prematch`）、没取到的、数据
    不全的。内置专家的委托回 `listed: false`。"""
    repo = workbench_store(session)
    try:
        await repo.get_task(task_id, owner_actor_id=_actor(principal))
    except WorkbenchError as exc:
        raise _http(exc) from exc
    row = await repo.get_task_resolution(task_id)
    pins = await repo.list_task_data(task_id)
    # 成稿没存进 knowbase 的写明原因（独立审 10-30 第二轮：用户在委托页看得到）
    outputs = [
        {"path": o["path"], "reason": o["last_error"]}
        for o in await repo.list_task_outputs(task_id)
        if o["status"] == "failed"
    ]
    if row is None:
        return {
            "contract_version": CONTRACT_VERSION,
            "listed": False,
            "outputs_not_saved": outputs,
        }
    detail = row.get("detail") or {}
    try:
        items, view_digest = await expert_data().pending_view(row, user_token)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    except Exception as exc:
        raise _http(UserDataUnavailable("暂时无法读取待确认的资料候选")) from exc
    shown = {
        e["ref"]: e["prematch"]
        for entries in items.values()
        for e in entries
        if e.get("prematch")
    }
    if view_digest is not None:
        async with repo.transaction():
            current = await repo.get_task_resolution(task_id, for_update=True)
            if current is None or current["detail"].get("requests") != detail.get(
                "requests"
            ):
                raise _http(StaleStateVersion("资料请求已变化，请刷新清单"))
            if current["detail"].get("view_digest") != view_digest:
                await repo.update_task_resolution(
                    task_id,
                    detail={
                        **current["detail"],
                        "view_digest": view_digest,
                        "prematch": shown,
                    },
                )
    return {
        "contract_version": CONTRACT_VERSION,
        "listed": True,
        "view_digest": view_digest,
        "outputs_not_saved": outputs,
        "status": row["status"],
        # 测试委托：提出的数据由作者在 knowlab 测试页确认（SDD 0046 第三节）
        "confirm_in": "knowlab" if row.get("resolution_id") is None else "here",
        "waiting_for": detail.get("waiting_for") or [],
        "demand_status": detail.get("demand_status"),
        "supply_progress": detail.get("supply_progress"),
        "reason": detail.get("reason"),
        "incomplete": detail.get("incomplete") or [],
        "requests": [
            {
                "id": r.get("id"),
                "labels": r.get("labels") or [],
                "review_dropped": r.get("review_dropped") or [],
                "review_picks": r.get("review_picks") or {},
                "status": r.get("status"),
                "requested_at": r.get("requested_at"),
                "needs": r.get("needs") or [],
                "items": items.get(str(r.get("id"))) or [],
                # 要去取的几项一共预计多久（预切合问到了才有，0043 第 3.3 节第 2 条）
                "eta_seconds": eta_of(items.get(str(r.get("id"))) or []),
                "missing": r.get("missing") or [],
            }
            for r in detail.get("requests") or []
            if r.get("status") != "merged"
        ],
        "pinned": [
            {
                "name": p["name"],
                "need": p["slot"],
                "version_no": p.get("version_no"),
                "basis": p.get("basis"),
            }
            for p in pins
        ],
    }


@router.post("/tasks/{task_id}/data-requests/confirm")
async def confirm_data_requests(
    task_id: str,
    body: ConfirmData,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    user_token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
) -> dict[str, Any]:
    """用户确认专家提出的数据（第 0 步或运行中，0048 第五节）：可以先改、换、去掉；
    齐的签上、钉上，缺的去取。"""
    repo = workbench_store(session)
    choice = body
    try:
        task = await repo.get_task(task_id, owner_actor_id=_actor(principal))
        try:
            done = await expert_data().confirm_requests(
                repo,
                task,
                owner_subject=principal.subject,
                user_token=user_token,
                view_digest=choice.view_digest,
                picks={k: v.model_dump() for k, v in choice.picks.items()},
                dropped=list(choice.dropped),
                needs=choice.needs,
            )
        except WorkbenchError:
            raise
        except Exception as exc:
            translated = refusal(exc)
            if translated is None:
                raise
            raise translated from None
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await publish_commands_wakeup()
    return {"contract_version": CONTRACT_VERSION, **done}


@router.post("/tasks/{task_id}/data-requests/recheck")
async def recheck_data_requests(
    task_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    user_token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
) -> dict[str, Any]:
    """「传好了，重新查」：用户去 knowbase 上传了采不来的那份，凭本人令牌请 knowbase 重解析。"""
    repo = workbench_store(session)
    try:
        task = await repo.get_task(task_id, owner_actor_id=_actor(principal))
        try:
            done = await expert_data().recheck(repo, task, user_token)
        except WorkbenchError:
            raise
        except Exception as exc:
            translated = refusal(exc)
            if translated is None:
                raise
            raise translated from None
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **done}


@router.post("/tasks/{task_id}/data-requests/decline")
async def decline_data_requests(
    task_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """用户不要专家提出的数据，或者没取到的照现有的接着做：委托接着做，记「数据不全」。"""
    repo = workbench_store(session)
    try:
        task = await repo.get_task(task_id, owner_actor_id=_actor(principal))
        done = await expert_data().decline_requests(repo, task)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    await publish_commands_wakeup()
    return {"contract_version": CONTRACT_VERSION, **done}


@router.get("/projects/{project_id}/board")
async def project_board(
    project_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """项目的看板：委托按列（排队、在跑、等你、等别人、完成、没成），卡片写清等谁、
    等什么（请专家方案设计第 4.8 节）。"""
    try:
        view = await ExpertDesk(workbench_store(session)).board(
            project_id, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **view}


@router.get("/expert/entry")
async def expert_entry(
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "private, no-store"
    return {"unified": find_expert_service(session).planning_enabled(principal.subject)}


@router.get("/expert/overview")
async def expert_overview(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """专家首页：等我决定、进行中、最近交回的。"""
    view = await ExpertDesk(workbench_store(session)).overview(
        owner_actor_id=_actor(principal)
    )
    return {"contract_version": CONTRACT_VERSION, **view}


@router.get("/interactions")
async def interactions(
    status: str = Query(default="pending", pattern=r"^(pending|consumed|all)$"),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """跨对话的待办。只给摘要；答复用的令牌不在这里。"""
    found = await ExpertDesk(workbench_store(session)).waiting_for_me(
        owner_actor_id=_actor(principal), status=None if status == "all" else status
    )
    return {"contract_version": CONTRACT_VERSION, "interactions": found}


@router.get("/interactions/{interaction_id}")
async def interaction(
    interaction_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """审查面：在哪、待决的是什么、决定的对象、时效、决定记录。"""
    try:
        view = await ExpertDesk(workbench_store(session)).review(
            interaction_id, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, "interaction": view}


# ---------- AI 请专家（请专家方案设计第 4.9 节；decisions 10-29「AI 请专家」） ----------
class FindExpertIn(BaseModel):
    """「让 AI 找」：写好的问题；费用上限是这一轮的（用户的模型、用户的钱）。"""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    budget_limit: Decimal = Field(default=Decimal("2"), gt=0, le=Decimal("100"))
    match_contract: Literal["knowbench/expert-match/v5"]
    reference: MethodRef | None = None
    retain_empty: bool = Field(default=False, strict=True)
    directory_offset: int = Field(default=0, strict=True, ge=0, le=2147483647)
    revises: UUID | None = None
    source_session: UUID | None = None


class FormalExpertReview(Strict):
    review_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class FormalExpertChoice(FormalExpertReview):
    reference: MethodRef


@router.post("/projects/{project_id}/find-expert", status_code=201)
async def start_find_expert(
    project_id: str,
    body: FindExpertIn,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """开「找专家」那一轮：用户的项目里、用户自己的模型读问题与专家的公开信息。"""
    try:
        made = await find_expert_service(session).start(
            owner_actor_id=_actor(principal),
            project_id=project_id,
            question=body.question,
            idempotency_key=body.idempotency_key,
            budget_limit=body.budget_limit,
            match_contract=body.match_contract,
            reference=body.reference,
            directory_offset=body.directory_offset,
            retain_empty=body.retain_empty,
            revises=str(body.revises) if body.revises else None,
            source_session=str(body.source_session) if body.source_session else None,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    if made.get("task_id"):
        await publish_commands_wakeup()
    return {"contract_version": CONTRACT_VERSION, **made}


@router.get("/find-expert/{task_id}/temporary-plan/audit")
async def planning_audit(
    task_id: UUID,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await temporary_planning.audit(
            find_expert_service(session), _actor(principal), str(task_id)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/find-expert/{task_id}/temporary-plan/materials")
async def planning_materials(
    task_id: UUID,
    review_digest: str,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
):
    response.headers["Cache-Control"] = "private, no-store"
    service = find_expert_service(session)
    owner = _actor(principal)
    try:
        async with service.repo.transaction():
            await temporary_planning.current(
                service.repo, owner, str(task_id), review_digest
            )
            if not service.planning_enabled(await service.repo.subject_of_actor(owner)):
                raise temporary_planning.NotFound("Planning is unavailable")
        return {"materials": await knowbase_v2().preparation_shelf(await token())}
    except WorkbenchError as exc:
        raise _http(exc) from exc
    except Exception as exc:
        translated = refusal(exc)
        if translated is None:
            raise
        raise _http(translated) from exc


@router.put("/find-expert/{task_id}/temporary-plan/probe")
async def inspect_planning_column(
    task_id: UUID,
    body: planning_probe.Probe,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
):
    try:
        return await planning_probe.run(
            find_expert_service(session),
            _actor(principal),
            str(task_id),
            body,
            knowbase_v2(),
            token,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    except Exception as exc:
        translated = refusal(exc)
        if translated is None:
            raise
        raise _http(translated) from exc


@router.put("/find-expert/{task_id}/temporary-plan")
async def start_temporary_plan(
    task_id: UUID,
    body: PlanningRequest,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
):
    try:
        result = await temporary_planning.start(
            find_expert_service(session),
            _actor(principal),
            str(task_id),
            body,
            library=knowbase_v2(),
            token=token,
        )
        if result["planning_task"]:
            await publish_commands_wakeup()
        return result
    except WorkbenchError as exc:
        raise _http(exc) from exc
    except Exception as exc:
        translated = refusal(exc)
        if translated is None:
            raise
        raise _http(translated) from exc


@router.get("/find-expert/{task_id}/temporary-plan")
async def read_temporary_plan(
    task_id: UUID,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await temporary_planning.sketch(
            find_expert_service(session), _actor(principal), str(task_id)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/temporary-plan/adoption")
async def adopt_temporary_plan(
    task_id: UUID,
    body: Adoption,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await temporary_planning.adopt(
            find_expert_service(session), _actor(principal), str(task_id), body
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/temporary-plan/saved")
async def save_temporary_plan(
    task_id: UUID,
    body: FormalExpertReview,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await temporary_planning.save(
            find_expert_service(session).repo,
            _actor(principal),
            str(task_id),
            body.review_digest,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/handoff")
async def transfer_formal_expert(
    task_id: UUID,
    body: Handoff,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await transfer(
            find_expert_service(session), _actor(principal), str(task_id), body
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/end")
async def end_unmatched_expert(
    task_id: UUID,
    body: FormalExpertReview,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    from app.application.workbench.formal_expert_intake import end_unmatched

    try:
        return await end_unmatched(
            find_expert_service(session).repo,
            _actor(principal),
            str(task_id),
            body.review_digest,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/selection")
async def confirm_formal_expert(
    task_id: UUID,
    body: FormalExpertChoice,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    from app.application.workbench.formal_expert_intake import confirm

    service = find_expert_service(session)
    try:
        return await confirm(
            service.repo, service.formal_registry, _actor(principal), str(task_id), body
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


async def _preparation(session, principal, task_id, token, **options):
    try:
        return await prepare(
            find_expert_service(session),
            _actor(principal),
            str(task_id),
            knowbase_v2(),
            token,
            **options,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    except Exception as exc:
        translated = refusal(exc)
        if translated is None:
            raise
        raise _http(translated) from exc


@router.get("/find-expert/{task_id}/preparation")
async def read_preparation(
    task_id: UUID,
    response: Response,
    library: bool = False,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await _preparation(session, principal, task_id, token, browse=library)


@router.put("/find-expert/{task_id}/preparation")
async def save_preparation(
    task_id: UUID,
    body: Preparation,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
):
    return await _preparation(session, principal, task_id, token, body=body)


@router.get("/find-expert/{task_id}/execution-preview")
async def read_execution_preview(
    task_id: UUID,
    response: Response,
    revision: int = Query(ge=1),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await _preparation(
        session, principal, task_id, token, preview_revision=revision
    )


@router.put("/find-expert/{task_id}/execution")
async def confirm_ordinary_execution(
    task_id: UUID,
    body: ExecutionConfirmation,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
):
    receipt = await _preparation(session, principal, task_id, token, submission=body)
    await publish_commands_wakeup()
    return receipt


@router.get("/tasks/{task_id}/handoffs")
async def read_child_handoffs(
    task_id: UUID,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await handoffs(workbench_store(session), _actor(principal), str(task_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/tasks/{task_id}/handoffs/{child_id}/artifacts/{artifact_id}")
async def read_child_evidence(
    task_id: UUID,
    child_id: UUID,
    artifact_id: UUID,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await handoff_evidence(
            workbench_store(session),
            _actor(principal),
            str(task_id),
            str(child_id),
            str(artifact_id),
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/tasks/{task_id}/handoffs")
async def close_child_handoffs(
    task_id: UUID,
    body: HandoffClosure,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await handoffs(
            workbench_store(session), _actor(principal), str(task_id), body
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/tasks/{task_id}/partial-delivery")
async def read_partial_delivery(
    task_id: UUID,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await partial_delivery(
            workbench_store(session), _actor(principal), str(task_id)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/tasks/{task_id}/partial-delivery")
async def receive_partial_delivery(
    task_id: UUID,
    body: BranchReceipt,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await partial_delivery(
            workbench_store(session), _actor(principal), str(task_id), body
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/find-expert/{task_id}")
async def find_expert_result(
    task_id: str,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """这一轮怎样了：直接请谁（不用另付钱的）/ 要付钱的候选 / 都不合适（推荐与理由、需求草稿）。"""
    try:
        response.headers["Cache-Control"] = "private, no-store"
        found = await find_expert_service(session).result(_actor(principal), task_id)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **found}


class DutyState(Strict):
    paused: bool


@router.get("/find-expert/{task_id}/duty")
async def read_duty(
    task_id: UUID,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    repo = workbench_store(session)
    response.headers["Cache-Control"] = "private, no-store"
    try:
        await repo.get_task(str(task_id), owner_actor_id=_actor(principal))
        return expert_duties.view(await repo.duty(str(task_id)))
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/duty")
async def create_duty(
    task_id: UUID,
    body: expert_duties.DutyConfig,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        if not get_settings().workbench_output_write_enabled:
            raise StaleStateVersion("KnowBase output writing is disabled")
        return await expert_duties.configure(
            find_expert_service(session), _actor(principal), str(task_id), body
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/duty/output")
async def approve_duty_output(
    task_id: UUID,
    body: expert_duties.ReleaseOutput,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        if not get_settings().workbench_output_write_enabled:
            raise StaleStateVersion("KnowBase output writing is disabled")
        return await expert_duties.release(
            find_expert_service(session),
            _actor(principal),
            str(task_id),
            str(body.execution_id),
            knowbase_v2(),
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    except Exception as exc:
        translated = refusal(exc)
        if translated is None:
            raise
        raise _http(translated) from exc


@router.put("/find-expert/{task_id}/duty/withdrawal")
async def withdraw_duty_output(
    task_id: UUID,
    body: expert_duties.ReleaseOutput,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await expert_duties.withdraw(
            find_expert_service(session),
            _actor(principal),
            str(task_id),
            str(body.execution_id),
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/duty/state")
async def pause_expert_duty(
    task_id: UUID,
    body: DutyState,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await expert_duties.set_paused(
            find_expert_service(session), _actor(principal), str(task_id), body.paused
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/find-expert/{task_id}/handoff/versions")
async def handoff_versions(
    task_id: UUID,
    response: Response,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await handoff_adoption.available(
            find_expert_service(session), _actor(principal), str(task_id)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put("/find-expert/{task_id}/handoff/versions")
async def adopt_handoff_version(
    task_id: UUID,
    body: handoff_adoption.AdoptVersion,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    try:
        saved = await handoff_adoption.adopt(
            find_expert_service(session), _actor(principal), str(task_id), body
        )
        await publish_commands_wakeup()
        return saved
    except WorkbenchError as exc:
        raise _http(exc) from exc
