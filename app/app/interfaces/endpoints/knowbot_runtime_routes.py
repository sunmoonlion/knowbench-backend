"""作为运行方跑分身测试的内部接口（SDD 0027 §3.4，契约 knowbot-test-run/v1）。

只接受 knowlab 的服务身份（关系 testrun，`knowbench:testrun`），身份先于数据库会话核。
入口统一在 `/api/internal/v1/knowbot-runtime`（traefik service-access 的内部入口）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.errors.exceptions import AppException
from app.application.workbench.test_runs import KnowbotTestService
from app.bootstrap.workbench import test_run_service
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.test_runs import RUN_ID, Answers, RunRequest
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_testrun_service,
)
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.internal.host_guard import internal_host_only
from core.config import get_settings

NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(
    prefix="/internal/v1/knowbot-runtime",
    tags=["Knowbot runtime"],
    dependencies=[Depends(internal_host_only)],  # 公网地址来的当没有（审读第 1 条）
)
Caller = Annotated[ServiceCaller, Depends(require_testrun_service)]
RunId = Annotated[str, Path(pattern=RUN_ID)]


def service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowbotTestService:
    return test_run_service(session, get_settings())


Service = Annotated[KnowbotTestService, Depends(service)]


def _http(exc: WorkbenchError) -> AppException:
    return AppException(code=exc.code, status_code=exc.http_status, msg=exc.message)


def _out(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=body, status_code=status_code, headers=NO_STORE)


@router.post("/test-runs")
async def submit(caller: Caller, body: dict[str, Any], svc: Service) -> JSONResponse:
    """受理一次测试：同一个 run_id 内容相同回原状态，不同则 409。"""
    try:
        request = RunRequest.model_validate(body)
    except ValidationError as exc:
        raise AppException(
            code="test_run_invalid", status_code=422, msg="not knowbot-test-run/v1"
        ) from exc
    try:
        result = await svc.accept(request, caller=caller.caller)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return _out(result, status_code=202)


@router.get("/test-runs/{run_id}")
async def status(_: Caller, svc: Service, run_id: RunId) -> JSONResponse:
    try:
        return _out(await svc.status(run_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post("/test-runs/{run_id}/answers")
async def answer(
    _: Caller, body: dict[str, Any], svc: Service, run_id: RunId
) -> JSONResponse:
    """回答待回答的问题：同一个问题重复回答，内容相同回原状态，不同则 409。"""
    try:
        answers = Answers.model_validate(body)
    except ValidationError as exc:
        raise AppException(
            code="test_answers_invalid", status_code=422, msg="not knowbot-test-run/v1"
        ) from exc
    try:
        return _out(await svc.answer(run_id, answers))
    except WorkbenchError as exc:
        raise _http(exc) from exc
