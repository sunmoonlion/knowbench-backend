"""委托钉住的数据（SDD 0043）：真数据库里记下、按次序读回、来源只有 knowbase、字段对得上才收。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.ledger import Ledger
from app.infrastructure.workbench.repository import WorkbenchRepository

BOUND = {
    "source": "knowbase",
    "slot": None,
    "item_id": "m-annual",
    "grant_id": "grant-1",
    "version_no": 1,
    "rendition_kind": "pages",
    "rendition_no": 1,
    "rendition_sha256": "c" * 64,
    "basis": "frozen_binding",
    "basis_ref": "expert:k@1",
    "name": "2024 年报.pdf",
    "media_type": "application/pdf",
    "size_bytes": 1024,
    "sha256": "a" * 64,
}
ASKED = {
    **BOUND,
    "slot": "利润表",
    "item_id": "m-income",
    "rendition_kind": "table",
    "basis": "public",
    "basis_ref": "public:disclosure",
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
            rows = await repo.pin_task_data(task, [BOUND])
        # 运行中补齐的（第四版 request_data）接在后面
        async with repo.transaction():
            rows = await repo.pin_task_data(task, [ASKED])
    async with db() as s:
        again = await WorkbenchRepository(s).list_task_data(task)
    assert [r["dataset_id"] for r in again] == [r["dataset_id"] for r in rows]
    assert [(r["position"], r["source"], r["slot"]) for r in again] == [
        (0, "knowbase", None),
        (1, "knowbase", "利润表"),
    ]
    assert (again[0]["item_id"], again[0]["grant_id"]) == ("m-annual", "grant-1")
    assert (again[0]["version_no"], again[0]["basis"]) == (1, "frozen_binding")
    assert again[1]["size_bytes"] == 2048 and again[1]["sha256"] == "b" * 64
    assert again[0]["grant_kind"] == "delegation" and "path" not in again[0]
    assert len({r["dataset_id"] for r in again}) == 2


@pytest.mark.parametrize(
    "bad",
    [
        {**BOUND, "grant_id": None},  # 没有运行授权
        {**BOUND, "item_id": None},  # 没有资料编号
        {**BOUND, "version_no": None},  # 没有内容版本
        {**BOUND, "basis": None},  # 没有权利依据
        {**BOUND, "source": "cloud"},  # v1 的云端知识库（decisions ⑰ 删了）
        {**BOUND, "source": "local"},  # 本地知识库（0043 第 4.1 节退役）
    ],
)
async def test_a_pin_must_match_its_source(db, bad) -> None:
    task = await _task(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        with pytest.raises(IntegrityError):
            async with repo.transaction():
                await repo.pin_task_data(task, [bad])
