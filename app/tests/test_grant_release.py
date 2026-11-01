"""委托结束后交还运行授权（grant_release）：按这件委托的授权 ref 一次交还（多签的一起撤，
独立审 10-30 第 5 条）；交还不成下一轮再来；真数据库里只交还已结束的委托的（测试的由 knowlab 撤）。"""

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
from app.infrastructure.external.knowbase_client import KnowbaseRefused
from app.infrastructure.workbench.repository import WorkbenchRepository


class Stores:
    def __init__(self, pending: list[str]) -> None:
        self.pending = [{"task_id": f"t-{ref}", "grant_ref": ref} for ref in pending]
        self.marked: list[str] = []
        self.notes: dict[str, str] = {}
        self.attempts: list[str] = []

    @contextlib.asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield self

    @contextlib.asynccontextmanager
    async def transaction(self):  # type: ignore[no-untyped-def]
        yield

    async def runs_to_release(self, *, limit: int = 50) -> list[dict]:
        return list(self.pending)

    async def mark_run_released(self, task_id: str, *, note=None) -> None:
        self.marked.append(task_id)
        if note:
            self.notes[task_id] = note

    async def note_release_attempt(self, task_id: str) -> None:
        self.attempts.append(task_id)


async def test_ended_tasks_hand_their_grants_back_and_failures_wait() -> None:
    released: list[str] = []

    async def release(ref: str) -> None:
        if ref == "k:down":
            raise UserDataUnavailable("down")
        if ref == "k:odd":
            raise RuntimeError("knowbase answered outside the contract")
        if ref == "k:limited":
            raise KnowbaseRefused(429, "rate_limited")
        if ref == "k:forbidden":
            raise KnowbaseRefused(403, "not_your_ref")
        if ref == "k:bad":
            raise KnowbaseRefused(422, "ref_invalid")
        released.append(ref)

    stores = Stores(
        ["k:1", "k:down", "k:odd", "k:limited", "k:forbidden", "k:bad", "k:2"]
    )
    assert await GrantReleaser(stores, release).sweep() == 2  # type: ignore[arg-type]
    assert released == ["k:1", "k:2"]
    # 只有明确的码（ref_invalid）记下放弃、不再占着；403、404、限流等照普通失败记次数、
    # 往后排着重试（独立审 10-30 第三轮第 4 条）
    assert stores.marked == ["t-k:1", "t-k:bad", "t-k:2"]
    assert stores.notes == {"t-k:bad": "knowbase 不收，放弃交还：ref_invalid"}
    assert stores.attempts == ["t-k:down", "t-k:odd", "t-k:limited", "t-k:forbidden"]


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
                ],
            )
            await repo.insert_task_resolution(
                {
                    "task_id": task,
                    "resolution_id": "res-1",
                    "revision": 1,
                    "consumer": "expert:k@1",
                    "params": {},
                    "grant_ref": "knowbench:k-release-1",
                    "status": "ready",
                }
            )
    released: list[str] = []

    async def release(ref: str) -> None:
        released.append(ref)

    releaser = GrantReleaser(workbench_stores(db), release)
    # 委托还在跑：不交还；结束了：按 ref 交还一次（这件委托签过的全部，含多签了没钉上的）
    assert await releaser.sweep() == 0
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set state = 'CANCELLED' where id = :t"),
            {"t": task},
        )
    assert await releaser.sweep() == 1
    assert await releaser.sweep() == 0
    assert released == ["knowbench:k-release-1"]
    async with db() as s:
        found = await s.execute(
            text(
                "select grant_id, grant_released_at is not null as done"
                " from workbench_task_data where task_id = :t and grant_id is not null"
            ),
            {"t": task},
        )
        assert {r.grant_id: r.done for r in found} == {"grant-1": True}


async def test_a_test_task_is_left_to_knowlab(db) -> None:  # noqa: F811
    """测试的委托（没有 knowbase 清单，授权由 knowlab 撤）不在这里交还。"""
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
            await repo.insert_task_resolution(
                {
                    "task_id": task,
                    "resolution_id": None,
                    "revision": None,
                    "consumer": "knowlab-test:run-1",
                    "params": {},
                    "grant_ref": "run-1:case-1",
                    "status": "planning",
                }
            )
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set state = 'CANCELLED' where id = :t"),
            {"t": task},
        )

    async def release(ref: str) -> None:  # pragma: no cover
        raise AssertionError(ref)

    assert await GrantReleaser(workbench_stores(db), release).sweep() == 0


async def test_a_task_still_waiting_or_being_checked_is_not_released_yet(db) -> None:  # noqa: F811
    """独立审 10-30 第二轮第 3 条：委托结束了、清单还在等数据或正有人领着查（可能正在续签）
    的先不交还，免得交还之后又签出一条；放开了再交还。交还不成的往后排。"""
    from datetime import UTC, datetime, timedelta

    _, _, sid = await seed(db)
    tasks = []
    for n in range(2):
        async with db() as s:
            made = await Ledger(WorkbenchRepository(s)).handover(
                handover(sid, key=f"k-wait-000{n}"), owner_actor_id=OWNER
            )
            tasks.append(made["task_id"])
        async with db() as s, s.begin():  # 同一段对话再交一件：先把方向盘还给用户
            await s.execute(
                text(
                    "update workbench_sessions set wheel = 'user',"
                    " active_task_id = null where id = :s"
                ),
                {"s": sid},
            )
    waiting, stuck = tasks
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            for task, status in ((waiting, "waiting"), (stuck, "ready")):
                await repo.insert_task_resolution(
                    {
                        "task_id": task,
                        "resolution_id": "res-1",
                        "revision": 1,
                        "consumer": "expert:k@1",
                        "params": {},
                        "grant_ref": f"knowbench:run:{task}",
                        "status": status,
                    }
                )
            await repo.claim_task_resolution(
                waiting,
                now=datetime.now(UTC),
                until=datetime.now(UTC) + timedelta(minutes=10),
            )
    async with db() as s, s.begin():
        await s.execute(
            text("update workbench_tasks set state = 'CANCELLED' where id = any(:t)"),
            {"t": tasks},
        )
    released: list[str] = []

    async def release(ref: str) -> None:
        if ref.endswith(stuck):
            raise UserDataUnavailable("down")
        released.append(ref)

    releaser = GrantReleaser(workbench_stores(db), release)
    assert await releaser.sweep() == 0  # 等数据、领着的不交还；卡住的记一次
    async with db() as s, s.begin():
        await s.execute(
            text(
                "update workbench_task_resolution set status = 'cancelled',"
                " checking_until = null where task_id = :t"
            ),
            {"t": waiting},
        )
    async with db() as s:
        order = await WorkbenchRepository(s).runs_to_release()
    # 交还不成过一次的排到后面
    assert [r["task_id"] for r in order] == [waiting, stuck]
    assert await releaser.sweep() == 1
    assert released == [f"knowbench:run:{waiting}"]
