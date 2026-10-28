"""委托结束后交还运行授权（grant_release）：交还不成下一轮再来；真数据库里只交还已结束的
委托的、请专家时签的那几条（测试的由 knowlab 撤）。"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import text
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db
from test_workbench_task_data_db import BOUND

from app.application.workbench.grant_release import GrantReleaser
from app.application.workbench.ledger import Ledger
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import UserDataUnavailable
from app.infrastructure.workbench.repository import WorkbenchRepository


class Stores:
    def __init__(self, pending: list[str]) -> None:
        self.pending = pending
        self.marked: list[str] = []

    @contextlib.asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield

    async def grants_to_release(self, *, limit: int = 50) -> list[str]:
        return list(self.pending)

    async def mark_grant_released(self, grant_id: str) -> None:
        self.marked.append(grant_id)


async def test_ended_tasks_hand_their_grants_back_and_failures_wait() -> None:
    released: list[str] = []

    async def release(grant_id: str) -> None:
        if grant_id == "g-down":
            raise UserDataUnavailable("down")
        if grant_id == "g-odd":
            raise RuntimeError("knowbase answered outside the contract")
        released.append(grant_id)

    stores = Stores(["g-1", "g-down", "g-odd", "g-2"])
    assert await GrantReleaser(stores, release).sweep() == 2  # type: ignore[arg-type]
    assert released == ["g-1", "g-2"] and stores.marked == ["g-1", "g-2"]


async def test_the_release_loop_stops_when_asked() -> None:
    async def release(grant_id: str) -> None:  # pragma: no cover
        raise AssertionError(grant_id)

    stop = asyncio.Event()
    releaser = GrantReleaser(Stores([]), release, interval_seconds=0.01)  # type: ignore[arg-type]
    running = asyncio.create_task(releaser.run_forever(stop))
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(running, 1)


async def test_only_ended_delegations_give_their_grants_back(db) -> None:  # noqa: F811
    _, _, sid = await seed(db)
    async with db() as s:
        task = (
            await Ledger(WorkbenchRepository(s)).handover(
                handover(sid), owner_actor_id=OWNER
            )
        )["task_id"]
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.pin_task_data(
                task,
                [
                    BOUND,
                    {**BOUND, "item_id": "m-2", "grant_id": "grant-1"},
                    {
                        **BOUND,
                        "item_id": "m-3",
                        "grant_id": "g-test",
                        "grant_kind": "test",
                    },
                ],
            )
    released: list[str] = []

    async def release(grant_id: str) -> None:
        released.append(grant_id)

    releaser = GrantReleaser(workbench_stores(db), release)
    # 委托还在跑：不交还；结束了：同一条授权交还一次；测试的不归这里
    assert await releaser.sweep() == 0
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set state = 'CANCELLED' where id = :t"),
            {"t": task},
        )
    assert await releaser.sweep() == 1
    assert await releaser.sweep() == 0
    assert released == ["grant-1"]
