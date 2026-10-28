"""委托结束后交还它的运行授权（runner 里一条线，隔一会儿扫一遍）。

钉住的数据（workbench_task_data）凭这件委托的运行授权读 knowbase；委托到了终态就交还
（knowbase user-data/v2 的交还接口），用户那边就不再记着这件委托在用。测试的委托
（`grant_kind` = test）由 knowlab 撤，这里不管。交还不成下一轮再来，授权到期也自己失效。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.application.ports.workbench import WorkbenchStore, WorkbenchStores
from app.domain.workbench.errors import WorkbenchError

log = logging.getLogger(__name__)


class GrantReleaser:
    def __init__(
        self,
        stores: WorkbenchStores,
        release: Callable[[str], Awaitable[None]],
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self.stores = stores
        self.release = release
        self.interval_seconds = interval_seconds

    async def sweep(self) -> int:
        async with self.stores() as repo:
            found = await repo.grants_to_release()
        released = 0
        for grant_id in found:
            try:
                await self.release(grant_id)
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
