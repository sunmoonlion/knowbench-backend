"""专家：有哪些专家、请专家、委托的步骤、底稿、等我决定的事（PRD/apps/investment-expert.md）。

方法的原文不出后端：这里的任何接口都不返回它。专家有两种来源：内置专家包，和用户从 knowmart
请来、在这里核验钉住的自己的分身（SDD 0027 P4，id 写作 `knowbot:<分身>@<版本>`）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench.catalog import (
    my_knowbots,
    pin_knowbot,
    viewable_expert,
)
from app.application.workbench.delegation import DelegationService
from app.application.workbench.expert_desk import ExpertDesk
from app.bootstrap.workbench import data_chooser, knowmart_summoner, workbench_store
from app.domain.security import Principal
from app.domain.workbench.data_needs import DataChoice
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.pack_view import pack_view
from app.domain.workbench.packs import listed_packs
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.workbench_routes import (
    CONTRACT_VERSION,
    _actor,
    _http,
    _publish_commands_wakeup,
    require_workbench_enabled,
)
from app.interfaces.http.middleware.auth import (
    get_web_current_user,
    get_web_knowbase_token_provider,
)

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
    # 写了数据需求声明的专家：按声明选的数据（SDD 0042）
    data: list[DataChoice] = Field(default_factory=list, max_length=64)


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
    mine = await my_knowbots(workbench_store(session), owner_actor_id=_actor(principal))
    return {
        "contract_version": CONTRACT_VERSION,
        # 默认预算与币种所有者还没定（W4）：先不给默认值，页面让用户自己填
        "budget": {"currency": "CNY", "default": None},
        "packs": [_view(p, "builtin") for p in listed_packs()]
        + [_view(p, "knowbot") for p in mine],
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


@router.get("/packs/{pack_id}/data-needs")
async def data_needs(
    pack_id: str,
    project_id: str | None = Query(default=None, max_length=64),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """请这位专家要提供哪些数据（数据需求声明），和同一项目上次选的那几份（弹框默认选上）。

    内置专家、旧格式的分身没有声明：needs 是空的，不用选。
    """
    try:
        found = await DelegationService(workbench_store(session)).data_needs(
            owner_actor_id=_actor(principal), profile_id=pack_id, project_id=project_id
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
        made = await DelegationService(workbench_store(session)).from_expert_entry(
            owner_actor_id=_actor(principal),
            project_id=project_id,
            profile_id=body.expert,
            question=question,
            budget_limit=body.budget_limit,
            budget_currency=body.budget_currency,
            idempotency_key=body.idempotency_key,
            data=[c.model_dump(exclude_none=True) for c in body.data],
            chooser=data_chooser(),
            user_token=user_token,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    if made["created"]:
        await _publish_commands_wakeup()
    return {"contract_version": CONTRACT_VERSION, **made}


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
