"""管理端：缺数据的需求（账 56）。要求 knowbench 管理员。

用户在对话里查不到数据的公司汇总在这里：代码、几次、几个人、最近一次、被查的数据集。
只汇总，不说是谁查的，只看：数据第四版起缺的数据由用户在委托里确认、knowfeed 替用户采。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench.demand import LIMIT, Demand
from app.bootstrap.workbench import workbench_store
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.middleware.auth import require_knowbench_admin

router = APIRouter(
    prefix="/admin/v1/workbench/missing-data",
    tags=["Admin missing data"],
    dependencies=[Depends(require_knowbench_admin)],
)


class MissingDataRead(BaseModel):
    security_code: str
    market: str | None
    times: int
    users: int
    last_at: datetime
    datasets: list[str]


class MissingDataListRead(BaseModel):
    items: list[MissingDataRead]


@router.get("", response_model=MissingDataListRead)
async def missing_data(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=LIMIT)] = LIMIT,
) -> dict:
    """各用户查不到数据的公司，最近的在前。"""
    items = await Demand(workbench_store(session)).summary(limit=limit)
    return {"items": items}
