"""预切合（契约 knowfeed-supply-prematch/v1，SDD 0043 第 3.3 节第 1 条、0045 第四节）。

确认之前，请专家清单上「待确认」的那几项先问 knowfeed：这类能不能取、网站待审没有、先试的
渠道是哪一档许可、按队列估要多久。确认面板（委托页与看板卡片）据此显示预计、许可档、
有没有渠道；用户确认时，回包连同我们可接受的条件照存进 knowbase 确认的供给条件快照
（`supply = {"conditions": ..., "prematch": <回包>}`），knowfeed 受理时只核 `conditions`。

契约的真源在 knowfeed（`knowfeed-app/contracts/supply-prematch/v1/`），本仓只持锁
（`knowbench-app/contracts/knowfeed-supply-prematch-lock.json`），测试核模型与提供方 schema
的字段一致。纯函数，不碰网络。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.workbench.data_request import DataRequest

CONTRACT = "knowfeed-supply-prematch/v1"
PATH = "/api/internal/v1/supply/prematch"
RELATION = "supply-demand"
# 一次最多问这么多项（提供方 items 的 maxItems）
MAX_ITEMS = 50

License = Literal["self", "via_expert", "sale"]
Reading = Literal["original", "text", "pages", "table"]

# 我们可接受的条件：请专家的数据只在本人的委托里用（专家在本人的账号、本人的电脑上跑，
# 作者看不到），「只许本人用」的也行，渠道不限（0045 第四节「可接受的来源限制与用途条件」）
CONDITIONS: dict[str, Any] = {"min_license": "self", "channels": []}

# 描述键里提供方收的几项（knowbase 的描述键多出来的不带，提供方 extra=forbid）
_KEY_FIELDS = (
    "subject",
    "doc_type",
    "period_start",
    "period_end",
    "edition",
    "topics",
    "industry",
    "source_query",
)


class PrematchKey(BaseModel):
    """问的一条描述（SDD 0049），同需求单的描述键。"""

    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, min_length=1, max_length=64)
    doc_type: str = Field(min_length=1, max_length=32)
    period_start: date | None = None
    period_end: date | None = None
    edition: Literal["full", "summary", "english"] = "full"
    topics: list[str] = Field(default_factory=list, max_length=12)
    industry: str | None = Field(default=None, max_length=16)
    source_query: dict[str, object] | None = None


class PrematchItem(BaseModel):
    """问的一项。请专家只问描述，不问网址（给网址的需求不从这里来）。"""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=64)
    key: PrematchKey | None = None
    url: str | None = Field(default=None, max_length=2000)
    readings: list[Reading] = Field(default_factory=list, max_length=4)


class Conditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_license: License | None = None
    channels: list[str] = Field(default_factory=list, max_length=20)


class PrematchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PrematchItem] = Field(min_length=1, max_length=MAX_ITEMS)
    conditions: Conditions = Field(default_factory=Conditions)


class Candidate(BaseModel):
    """一条候选渠道（按切合时试的先后）。"""

    channel_id: str
    name: str
    license: License
    status: Literal["active", "paused"]
    readings: list[Reading]
    eta_seconds: int | None


class PrematchAnswer(BaseModel):
    ref: str
    status: Literal["available", "unavailable", "site_pending", "conditions_unmet"]
    reason: str | None = None
    license: License | None = None
    eta_seconds: int | None = None
    readings_missing: list[Reading] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)


class PrematchResponse(BaseModel):
    contract: Literal["knowfeed-supply-prematch/v1"] = CONTRACT
    items: list[PrematchAnswer]


def request_items(
    entries: list[dict[str, Any]], needs: list[DataRequest]
) -> list[dict[str, Any]]:
    """清单里待确认的条目 → 要问的几项（条目带 `ref`）。没有主体的（不限主体）不问：
    knowbase 对它回「暂不能补采」，不会进确认。读法照那一样要的。"""
    by_key = {n.key: n for n in needs}
    out: list[dict[str, Any]] = []
    for entry in entries:
        key = entry.get("key") or {}
        if entry.get("status") != "to_confirm" or not (
            key.get("subject") or key.get("source_query")
        ):
            continue
        need = by_key.get(str(entry.get("need_key")))
        given = {f: key[f] for f in _KEY_FIELDS if key.get(f) not in (None, [])}
        try:
            item = PrematchItem(
                ref=str(entry["ref"]),
                key=PrematchKey.model_validate(given),
                readings=[need.reading] if need is not None else [],
            )
        except ValidationError:
            # 提供方不收的写法（太长之类）：这一项不问，面板上照常列、只是没有预计
            continue
        out.append(item.model_dump(mode="json", exclude_none=True))
    return out


def supply_snapshot(response: dict[str, Any] | None) -> dict[str, Any]:
    """确认时交 knowbase 的供给条件快照；预切合没问成就只有条件。"""
    snapshot: dict[str, Any] = {"conditions": dict(CONDITIONS)}
    if response is not None:
        snapshot["prematch"] = response
    return snapshot


def eta_of(entries: list[dict[str, Any]]) -> int | None:
    """这张清单要去取的几项一共预计多久：保守地按全部要新取算，逐项相加（0043 第 3.3 节
    第 2 条）。能取的里有一项算不出（渠道暂停）就不写；没有能取的也不写。"""
    found = [
        e["prematch"]
        for e in entries
        if isinstance(e.get("prematch"), dict)
        and e["prematch"].get("status") == "available"
    ]
    if not found or any(p.get("eta_seconds") is None for p in found):
        return None
    return sum(int(p["eta_seconds"]) for p in found)
