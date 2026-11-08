"""作为运行方的内部接口：跑分身测试（SDD 0027 §3.4，契约 knowbot-test-run/v1）与跑孵化的
一轮（专家孵化方案第八节，契约 knowbot-incubation/v1）。

只接受 knowlab 的服务身份：测试是关系 testrun（`knowbench:testrun`），孵化是关系 incubate
（`knowbench:incubate`），两种身份各管各的接口，身份先于数据库会话核。入口统一在
`/api/internal/v1/knowbot-runtime`（traefik service-access 的内部入口）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.errors.exceptions import AppException
from app.application.workbench.incubations import IncubationService
from app.application.workbench.test_execution import execution_view
from app.application.workbench.test_runs import KnowbotTestService
from app.bootstrap.workbench import incubation_service, test_run_service
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.incubations import INCUBATION_ID, IncubationRequest
from app.domain.workbench.test_runs import CASE_ID, RUN_ID, Answers, RunRequest
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_incubate_service,
    require_testrun_service,
)
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.commands_wakeup import publish_commands_wakeup
from app.interfaces.http.internal.host_guard import internal_host_only

NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(
    prefix="/internal/v1/knowbot-runtime",
    tags=["Knowbot runtime"],
    dependencies=[Depends(internal_host_only)],  # 公网地址来的当没有（审读第 1 条）
)
Caller = Annotated[ServiceCaller, Depends(require_testrun_service)]
RunId = Annotated[str, Path(pattern=RUN_ID)]
CaseId = Annotated[str, Path(pattern=CASE_ID)]


def service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> KnowbotTestService:
    return test_run_service(session)


Service = Annotated[KnowbotTestService, Depends(service)]
IncubationCaller = Annotated[ServiceCaller, Depends(require_incubate_service)]
IncubationId = Annotated[str, Path(pattern=INCUBATION_ID)]


def incubations(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> IncubationService:
    return incubation_service(session)


Incubations = Annotated[IncubationService, Depends(incubations)]


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
            code="test_run_invalid", status_code=422, msg="unsupported test-run request"
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


@router.get("/test-runs/{run_id}/cases/{case_id}/outputs")
async def outputs(
    _: Caller, svc: Service, run_id: RunId, case_id: CaseId
) -> JSONResponse:
    """一条用例跑完时取好的成稿与各步交回物（SDD 0037）；还没有就 404。"""
    try:
        return _out(await svc.outputs(run_id, case_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/test-runs/{run_id}/execution")
async def execution(
    _: Caller,
    svc: Service,
    run_id: RunId,
    owner_subject: Annotated[str, Query(min_length=1, max_length=512)],
    case_id: Annotated[str | None, Query(pattern=CASE_ID)] = None,
) -> JSONResponse:
    """knowlab authorizes the viewer; runtime also checks owner/run/case binding."""
    try:
        return _out(
            jsonable_encoder(
                await execution_view(
                    svc.repo, run_id, owner_subject=owner_subject, case_id=case_id
                )
            )
        )
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


@router.post("/test-runs/{run_id}/cancel")
async def cancel(_: Caller, svc: Service, run_id: RunId) -> JSONResponse:
    """停下这次测试（10-30 加：作者撤回交付、平台审成不过时停平台重跑）：在跑的用例委托
    停下、授权交还、记成取消；已经结束的回原状态。回 RunResultV1。"""
    try:
        result = await svc.cancel(run_id)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    # 叫醒 runner 去中断作者电脑上在跑的那一轮（同网页上停委托）
    await publish_commands_wakeup()
    return _out(result)


@router.get("/users/{subject}/computers")
async def computers(
    _: Caller,
    svc: Service,
    subject: Annotated[str, Path(pattern=r"^[A-Za-z0-9._:@-]{1,512}$")],
) -> JSONResponse:
    """用户接入过的电脑（SDD 0046 第三节「怎么选电脑」）：knowlab 列给他选，在哪台跑自己的测试。"""
    return _out({"computers": await svc.computers(subject)})


@router.get("/users/{subject}/readiness")
async def readiness(
    _: Caller,
    svc: Service,
    subject: Annotated[str, Path(pattern=r"^[A-Za-z0-9._:@-]{1,512}$")],
) -> JSONResponse:
    """用户填了能用的模型 key 没有（SDD 0046 第三节「没填不能发起」）：knowlab 发起测试、
    孵化前先问，没填就在页面上拦，不等跑起来才停在「没有沙箱」。"""
    return _out(await svc.readiness(subject))


@router.post("/incubations")
async def submit_incubation(
    caller: IncubationCaller, body: dict[str, Any], svc: Incubations
) -> JSONResponse:
    """受理孵化的一轮：同一个 incubation_id 内容相同回原状态，不同则 409。"""
    try:
        request = IncubationRequest.model_validate(body)
    except ValidationError as exc:
        raise AppException(
            code="incubation_invalid",
            status_code=422,
            msg="not knowbot-incubation/v1",
        ) from exc
    try:
        result = await svc.accept(request, caller=caller.caller)
    except WorkbenchError as exc:
        raise _http(exc) from exc
    return _out(result, status_code=202)


@router.get("/incubations/{incubation_id}")
async def incubation_status(
    _: IncubationCaller, svc: Incubations, incubation_id: IncubationId
) -> JSONResponse:
    try:
        return _out(await svc.status(incubation_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post("/incubations/{incubation_id}/cancel")
async def cancel_incubation(
    _: IncubationCaller, svc: Incubations, incubation_id: IncubationId
) -> JSONResponse:
    """取消：在跑的委托停下；已经结束的回原状态。"""
    try:
        return _out(await svc.cancel(incubation_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc
