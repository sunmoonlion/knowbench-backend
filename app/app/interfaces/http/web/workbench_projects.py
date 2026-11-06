"""工作区、项目、对话的归属与种类（PRD/apps/investment.md 7.3）。登录的用户；只碰得到自己的。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench.agent_service import AgentService
from app.application.workbench.project_service import ProjectService, plain
from app.application.workbench.session_service import SessionService
from app.bootstrap.workbench import web_workbench_store as workbench_store
from app.domain.security import Principal
from app.domain.workbench.errors import WorkbenchError
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.workbench_routes import (
    CONTRACT_VERSION,
    _actor,
    _http,
    optional_agent_service,
    require_workbench_enabled,
)
from app.interfaces.http.middleware.auth import get_web_current_user

router = APIRouter(
    prefix="/workbench",
    tags=["Workbench"],
    dependencies=[Depends(require_workbench_enabled)],
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProject(Strict):
    environment_id: str = Field(max_length=64)
    workspace_root: str = Field(min_length=1, max_length=1024)
    path: str = Field(default="", max_length=1024)
    title: str | None = Field(default=None, max_length=400)


class ChangeProject(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=400)
    archived: bool | None = None


class AttachProject(Strict):
    project_id: str = Field(max_length=64)


class ChangeKind(Strict):
    kind: str = Field(pattern=r"^work$")  # 只许聊天转工作


class ChangeConversation(Strict):
    title: str | None = Field(default=None, max_length=400)


@router.get("/workspaces")
async def workspaces(
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """我的工作区：每台机器的每个根目录。增减在用户机器上的本地代理里做。"""
    service = ProjectService(workbench_store(session))
    return {
        "contract_version": CONTRACT_VERSION,
        "workspaces": await service.workspaces(owner_actor_id=_actor(principal)),
    }


@router.get("/projects")
async def list_projects(
    include_archived: bool = Query(default=False),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    service = ProjectService(workbench_store(session))
    return {
        "contract_version": CONTRACT_VERSION,
        "projects": await service.listing(
            owner_actor_id=_actor(principal), include_archived=include_archived
        ),
    }


@router.post("/projects", status_code=201)
async def create_project(
    body: CreateProject,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
    agent: AgentService | None = Depends(optional_agent_service),
) -> dict[str, Any]:
    try:
        return await ProjectService(workbench_store(session), agent).create(
            owner_actor_id=_actor(principal),
            environment_id=body.environment_id,
            workspace_root=body.workspace_root,
            path=body.path,
            title=body.title,
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/projects/{project_id}")
async def get_project(
    project_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        view = await ProjectService(workbench_store(session)).view(
            project_id, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return {"contract_version": CONTRACT_VERSION, **view}


@router.patch("/projects/{project_id}")
async def change_project(
    project_id: str,
    body: ChangeProject,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """改名、归档、恢复。"""
    if body.title is None and body.archived is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    service, actor = ProjectService(workbench_store(session)), _actor(principal)
    try:
        if body.title is not None:
            project = await service.rename(
                project_id, owner_actor_id=actor, title=body.title
            )
        if body.archived is not None:
            project = await service.archive(
                project_id, owner_actor_id=actor, archived=body.archived
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return project


@router.delete("/projects/{project_id}", status_code=204)
async def remove_project(
    project_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """侧边栏的「删除」：从工作台移除（= 归档）。用户电脑上的文件夹不动（所有者 2026-10-12 定）。"""
    try:
        await ProjectService(workbench_store(session)).archive(
            project_id, owner_actor_id=_actor(principal), archived=True
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204)
async def remove_conversation(
    session_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """侧边栏的「删除」：从工作台移除这段对话；历史与审批日志留着，不再列出。"""
    try:
        await SessionService(workbench_store(session)).archive(
            session_id, owner_actor_id=_actor(principal), archived=True
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return Response(status_code=204)


@router.post("/sessions/{session_id}/project")
async def attach_project(
    session_id: str,
    body: AttachProject,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """把不属于项目的聊天放进一个项目。放进去之后不能换。"""
    try:
        changed = await SessionService(workbench_store(session)).attach_project(
            session_id, owner_actor_id=_actor(principal), project_id=body.project_id
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return plain(changed)


@router.post("/sessions/{session_id}/kind")
async def change_kind(
    session_id: str,
    body: ChangeKind,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """聊天转为工作。同一条线，之前聊的还在。"""
    try:
        changed = await SessionService(workbench_store(session)).to_work(
            session_id, owner_actor_id=_actor(principal)
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return plain(changed)


@router.patch("/sessions/{session_id}")
async def change_conversation(
    session_id: str,
    body: ChangeConversation,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        changed = await SessionService(workbench_store(session)).rename(
            session_id, owner_actor_id=_actor(principal), title=body.title
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return plain(changed)
