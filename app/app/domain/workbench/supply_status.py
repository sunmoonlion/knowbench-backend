"""knowfeed 推来的需求状态（契约 knowfeed-supply-status/v1，SDD 0045 第 2.5 节「需求状态只由
knowfeed 通知」）。

契约的真源在 knowfeed（`knowfeed-app/contracts/supply-status/v1/`），本仓只持锁
（`knowbench-app/contracts/knowfeed-supply-status-lock.json`），测试核模型与提供方 schema 一致。
一条通知带这张单当时的汇总状态与每一项；通知可能重发、可能晚到，工作台只拿它当「该查了」，
以按单号查到的为准。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT = "knowfeed-supply-status/v1"
RECEIVER_PATH = "/api/internal/v1/knowfeed/demand-status"
RELATION = "supply-status"

DemandStatus = Literal[
    "accepted",
    "fetching",
    "ready",
    "partial",
    "failed",
    "unsatisfiable",
    "cancelled",
    "timeout",
]
# 还在办的：等数据的委托照旧等
OPEN = frozenset({"accepted", "fetching"})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DemandStatusItem(Strict):
    item_id: str = Field(min_length=1, max_length=64)
    confirmation_item: str = Field(min_length=1, max_length=16)
    status: DemandStatus
    reason: str | None = Field(default=None, max_length=200)
    updated_at: datetime


class DemandStatusEvent(Strict):
    contract: Literal["knowfeed-supply-status/v1"] = CONTRACT
    event_id: str = Field(min_length=1, max_length=64)
    demand_id: str = Field(min_length=1, max_length=64)
    app: Literal["knowbench", "knowlab"]
    owner: str = Field(min_length=1, max_length=255)
    task_ref: str | None = Field(default=None, max_length=255)
    status: DemandStatus
    items: tuple[DemandStatusItem, ...] = Field(min_length=1, max_length=50)
    occurred_at: datetime

    @property
    def settled(self) -> bool:
        """这张单办完了（取到、取到一部分、取不来、撤了、超时），等它的委托该接着走了。"""
        return self.status not in OPEN
