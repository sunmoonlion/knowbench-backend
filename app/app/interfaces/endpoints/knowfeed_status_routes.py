"""收 knowfeed 推来的需求状态（契约 knowfeed-supply-status/v1，SDD 0045 第 2.5 节「需求状态
只由 knowfeed 通知」）。

只认 knowfeed 的服务身份（关系 supply-status，scope `knowbench:supply-status`），身份先于
数据库会话核。需求单办完了：把等它的委托领下来，回过 knowfeed 后马上查这一张、重解析、接着跑
（`DataWaiter`）；还在办的不动。这条推送只当「该查了」，以按单号查到的为准；进程中途停了，
领下的那几件过一会儿照旧由 runner 里到点查的线兜底。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.application.errors.exceptions import AppException
from app.domain.workbench.supply_status import DemandStatusEvent
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_supply_status_service,
)
from app.interfaces.http.internal.host_guard import internal_host_only

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/v1/knowfeed",
    tags=["Knowfeed supply status"],
    dependencies=[Depends(internal_host_only)],  # 公网地址来的当没有（审读第 1 条）
)
Caller = Annotated[ServiceCaller, Depends(require_supply_status_service)]


def data_waiter():
    """等数据的那条线的同一套查法（knowbase、knowfeed 客户端进程内共用）。"""
    from app.bootstrap.workbench import build_data_waiter
    from app.infrastructure.storage.postgres import get_postgres

    return build_data_waiter(get_postgres().session_factory)


Waiter = Annotated[Any, Depends(data_waiter)]


@router.post("/demand-status")
async def demand_status(
    _: Caller, body: dict[str, Any], background: BackgroundTasks, waiter: Waiter
) -> JSONResponse:
    try:
        event = DemandStatusEvent.model_validate(body)
    except ValidationError as exc:
        raise AppException(
            code="supply_status_invalid",
            status_code=422,
            msg="not knowfeed-supply-status/v1",
        ) from exc
    if event.app != "knowbench":
        raise AppException(
            code="supply_status_not_ours",
            status_code=422,
            msg="this demand was not raised by knowbench",
        )
    claimed: list[dict[str, Any]] = []
    if event.settled:
        claimed = await waiter.claim(event.demand_id, owner_subject=event.owner)
        if claimed:
            background.add_task(waiter.check_now, claimed)
    log.info(
        "demand status received demand=%s status=%s claimed=%d",
        event.demand_id,
        event.status,
        len(claimed),
    )
    return JSONResponse(
        content={"event_id": event.event_id, "claimed": len(claimed)},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )
