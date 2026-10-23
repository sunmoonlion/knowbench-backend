"""委托钉住的数据（SDD 0042 第五节）：真数据库里记下、按次序读回、来源与字段对得上才收。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.ledger import Ledger
from app.infrastructure.workbench.repository import WorkbenchRepository

LOCAL = {
    "source": "local",
    "slot": "年报",
    "path": "C:\\资料\\2024 年报.pdf",
    "name": "2024 年报.pdf",
    "media_type": "application/pdf",
    "size_bytes": 1024,
    "sha256": "a" * 64,
}
CLOUD = {
    "source": "cloud",
    "slot": "利润表",
    "item_id": "item-1",
    "grant_id": "grant-1",
    "name": "利润表.xlsx",
    "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "size_bytes": 2048,
    "sha256": "b" * 64,
}


async def _task(db) -> str:  # type: ignore[no-untyped-def]
    _, _, sid = await seed(db)
    async with db() as s:
        return (
            await Ledger(WorkbenchRepository(s)).handover(
                handover(sid), owner_actor_id=OWNER
            )
        )["task_id"]


async def test_pins_are_kept_in_order_with_their_own_ids(db) -> None:
    task = await _task(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        assert await repo.list_task_data(task) == []
        async with repo.transaction():
            rows = await repo.pin_task_data(task, [LOCAL, CLOUD])
    async with db() as s:
        again = await WorkbenchRepository(s).list_task_data(task)
    assert [r["dataset_id"] for r in again] == [r["dataset_id"] for r in rows]
    assert [(r["position"], r["source"], r["slot"]) for r in again] == [
        (0, "local", "年报"),
        (1, "cloud", "利润表"),
    ]
    assert again[0]["path"] == LOCAL["path"] and again[0]["item_id"] is None
    assert (again[1]["item_id"], again[1]["grant_id"]) == ("item-1", "grant-1")
    assert again[1]["size_bytes"] == 2048 and again[1]["sha256"] == "b" * 64
    assert len({r["dataset_id"] for r in again}) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {**CLOUD, "grant_id": None},  # 云端没有运行授权
        {**LOCAL, "path": None},  # 本地没有路径
        {**LOCAL, "item_id": "item-1"},  # 本地混了云端的字段
        {**CLOUD, "source": "nas"},  # 不认的来源
    ],
)
async def test_a_pin_must_match_its_source(db, bad) -> None:
    task = await _task(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        with pytest.raises(IntegrityError):
            async with repo.transaction():
                await repo.pin_task_data(task, [bad])
