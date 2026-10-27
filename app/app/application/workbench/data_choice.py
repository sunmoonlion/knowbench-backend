"""请专家时选数据、钉住（SDD 0042 第五节第 4、6 项）——清单 v1 专家的兼容分支。

清单 v2 的专家改走解析（expert_data.py）。v1 的专家到「财务分析与预测」第 2 版上架为止
照旧逐样从云端知识库选（SDD 0043 第 3.1 节兼容分支）：凭用户本人的令牌向 knowbase 核这一份
在他的 knowbench 分区里，记下名字、类型、大小、摘要；选好的几份签一条这件委托的运行授权。
本地知识库（经 link 读本地文件）已退役（0043 第 4.1 节），新请求里选本地的一律拒；
老委托里钉的本地来源只读。

按声明核过（必填不缺、份数、格式、不重复）才钉；钉进委托在建委托的同一次提交里（Ledger）。
建委托没成就把刚签的授权交还。委托结束后由 runner 交还授权（GrantReleaser），用户就能删了。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.application.ports.workbench import CloudGrants, WorkbenchStore, WorkbenchStores
from app.domain.workbench.data_needs import (
    DataNeed,
    choice_problems,
    format_of,
)
from app.domain.workbench.errors import (
    DataChoiceInvalid,
    DataRequired,
    WorkbenchError,
)

log = logging.getLogger(__name__)

CLOUD = "cloud"
LOCAL = "local"
MAX_FILE_BYTES = 50 * 1024 * 1024  # 与 knowbase 收的一致
# 委托的运行授权：knowbase 最长给 7 天；委托结束由 runner 交还，不等到期
GRANT_TTL_SECONDS = 7 * 24 * 3600

UserToken = Callable[[], Awaitable[str]]


@dataclass
class Chosen:
    """钉住前的一份：pin 是要写进 workbench_task_data 的那一行（grant_id 签了授权再补）。"""

    pin: dict[str, Any]
    format: str | None
    identity: str


@dataclass
class Prepared:
    pins: list[dict[str, Any]] = field(default_factory=list)
    grant_id: str | None = None


def choice_of(raw: dict[str, Any]) -> dict[str, Any]:
    """请求里的一份选择，规整成只留认得的字段（也是进幂等摘要的那一份）。"""
    if raw.get("source") == CLOUD:
        return {"need": raw.get("need"), "source": CLOUD, "item_id": raw.get("item_id")}
    return {"need": raw.get("need"), "source": LOCAL, "path": raw.get("path")}


class DataChooser:
    def __init__(self, *, cloud: CloudGrants) -> None:
        self.cloud = cloud

    async def prepare(
        self,
        *,
        needs: list[DataNeed],
        choices: list[dict[str, Any]],
        relay_user: str | None,
        user_token: UserToken,
        ref: str,
    ) -> Prepared:
        """核用户选的、签授权，回要钉的那几行。没有声明的专家不收数据。"""
        if not needs:
            if choices:
                raise DataChoiceInvalid("this expert takes no data")
            return Prepared()
        if not choices:
            raise DataRequired("choose the data this expert needs first")
        if any(choice_of(raw)["source"] != CLOUD for raw in choices):
            raise DataChoiceInvalid(
                "local data sources are retired; put the file in your cloud knowledge base"
            )
        token = await user_token()
        chosen = [
            await self._cloud(token, choice_of(raw), position)
            for position, raw in enumerate(choices)
        ]
        problems = choice_problems(
            needs,
            [
                {"need": c.pin["slot"], "format": c.format, "identity": c.identity}
                for c in chosen
            ],
        )
        if problems:
            raise DataChoiceInvalid("; ".join(problems))
        cloud = [c.pin for c in chosen if c.pin["source"] == CLOUD]
        grant_id = None
        if cloud:
            assert token is not None
            grant_id = await self.cloud.grant(
                token,
                ref=ref,
                items=[{"item_id": p["item_id"], "sha256": p["sha256"]} for p in cloud],
                ttl_seconds=GRANT_TTL_SECONDS,
            )
            for pin in cloud:
                pin["grant_id"] = grant_id
        return Prepared(pins=[c.pin for c in chosen], grant_id=grant_id)

    async def release(self, prepared: Prepared) -> None:
        """建委托没成：刚签的授权交还（交还不成也不挡，授权到期自己失效）。"""
        if prepared.grant_id is None:
            return
        try:
            await self.cloud.release(prepared.grant_id)
        except WorkbenchError:
            log.warning("could not hand back an unused grant %s", prepared.grant_id)

    async def _cloud(self, token: str, choice: dict[str, Any], position: int) -> Chosen:
        item_id = choice.get("item_id")
        if not isinstance(item_id, str) or not item_id or len(item_id) > 64:
            raise DataChoiceInvalid("a cloud item is not named")
        item = await self.cloud.item(token, item_id)
        size = int(item.get("size_bytes") or 0)
        if size > MAX_FILE_BYTES:
            raise DataChoiceInvalid(f"{item.get('name')} is larger than 50 MB")
        media_type = str(item.get("media_type") or "")
        return Chosen(
            pin={
                "position": position,
                "slot": choice.get("need"),
                "source": CLOUD,
                "item_id": item_id,
                "path": None,
                "name": str(item.get("name") or item_id)[:255],
                "media_type": media_type,
                "size_bytes": size,
                "sha256": str(item.get("sha256") or ""),
            },
            format=format_of(media_type),
            identity=f"cloud:{item_id}",
        )


def last_choices(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """上次钉住的几份，变成网页弹框里默认选上的样子。"""
    out = []
    for p in pins:
        item: dict[str, Any] = {
            "need": p["slot"],
            "source": p["source"],
            "name": p["name"],
        }
        if p["source"] == CLOUD:
            item["item_id"] = p["item_id"]
        else:
            item["path"] = p["path"]
        out.append(item)
    return out


class GrantReleaser:
    """委托结束后交还它的运行授权（runner 里一条线，隔一会儿扫一遍）。"""

    def __init__(
        self,
        stores: WorkbenchStores,
        cloud: CloudGrants,
        *,
        release_v2: Callable[[str], Awaitable[None]] | None = None,
        interval_seconds: float = 30.0,
    ) -> None:
        self.stores = stores
        self.cloud = cloud
        # 第四版（钉住来源 knowbase）的授权走 v2 的交还接口
        self.release_v2 = release_v2
        self.interval_seconds = interval_seconds

    async def sweep(self) -> int:
        async with self.stores() as repo:
            found = await repo.grants_to_release()
        released = 0
        for row in found:
            grant_id = str(row["grant_id"])
            try:
                if row.get("source") == "knowbase":
                    if self.release_v2 is None:
                        continue
                    await self.release_v2(grant_id)
                else:
                    await self.cloud.release(grant_id)
            except WorkbenchError as exc:
                log.warning("grant release failed grant=%s code=%s", grant_id, exc.code)
                continue
            except Exception as exc:  # noqa: BLE001 — 交还不成下一轮再来
                log.warning(
                    "grant release failed grant=%s error=%s",
                    grant_id,
                    getattr(exc, "code", type(exc).__name__),
                )
                continue
            async with self.stores() as repo:
                await _mark(repo, grant_id)
            released += 1
        return released

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("grant release starting interval=%ss", self.interval_seconds)
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 — 这条线不能因为一次出错就停
                log.exception("grant release sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass


async def _mark(repo: WorkbenchStore, grant_id: str) -> None:
    async with repo.transaction():
        await repo.mark_grant_released(grant_id)
