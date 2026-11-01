"""委托结束后交还它的运行授权（runner 里一条线，隔一会儿扫一遍）。

钉住的数据（workbench_task_data）凭这件委托的运行授权读 knowbase；委托到了终态就按这件委托的
授权 ref 一次交还（knowbase user-data/v2 `runs/{ref}/release`）：分几次签的、多签了没钉上的
（推送与到点查同时续签过）一起撤掉，用户那边就不再记着这件委托在用。测试的委托由 knowlab 撤，
这里不管。还在等数据、或正有人领着查（可能正在续签）的先不交还（repository `runs_to_release`）。
交还不成下一轮再来、往后排；只有 knowbase 明确说这个 ref 不成立（`ref_invalid` 这类码）才记下
放弃的原因、不再占着，403、404 这些照普通失败往后排着重试（可能是配置、发版顺序的事）；
授权到期也自己失效。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.application.ports.workbench import WorkbenchStore, WorkbenchStores

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
        # 按 ref 交还：knowbase 的 release_run(ref)
        self.release = release
        self.interval_seconds = interval_seconds

    async def sweep(self) -> int:
        async with self.stores() as repo:
            found = await repo.runs_to_release()
        released = 0
        for run in found:
            try:
                await self.release(run["grant_ref"])
            except Exception as exc:  # noqa: BLE001 — 交还不成下一轮再来
                code = str(getattr(exc, "code", "") or type(exc).__name__)
                log.warning(
                    "grant release failed task=%s code=%s", run["task_id"], code
                )
                async with self.stores() as repo:
                    async with repo.transaction():
                        if _permanent(exc):
                            await repo.mark_run_released(
                                run["task_id"], note=f"knowbase 不收，放弃交还：{code}"
                            )
                        else:
                            await repo.note_release_attempt(run["task_id"])
                continue
            async with self.stores() as repo:
                await _mark(repo, run["task_id"])
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


# knowbase 明确说这个 ref 交还不了、再试也不会成的码（knowbase check_ref）
GIVE_UP_CODES = frozenset({"ref_invalid"})


def _permanent(exc: Exception) -> bool:
    """只认明确的码；别的 4xx（403、404……）照普通失败重试。"""
    return str(getattr(exc, "code", "") or "") in GIVE_UP_CODES


async def _mark(repo: WorkbenchStore, task_id: str) -> None:
    async with repo.transaction():
        await repo.mark_run_released(task_id)
