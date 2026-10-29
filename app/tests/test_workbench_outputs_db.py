"""委托交付的成稿替用户写进 knowbase 产出区（SDD 0043 第 2.6 节）：真数据库，用户电脑与 knowbase 用替身。

委托成功时每份成稿记一行待写；runner 里那条线读用户电脑上的成稿、以服务身份写进产出区，带出处；
写不进去不挡交付，电脑不在线、knowbase 不通就隔一阵再试，knowbase 明说不收就停下、原因留着。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from test_workbench_advisor_db import ANSWER, PLAN, ScriptedAppServer, drive, seed
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.workbench.deliverable_check import (
    DeliverableTooLarge,
    ReadFile,
)
from app.application.workbench.outputs import (
    GIVE_UP,
    RETRY,
    OutputWriter,
    expert_of,
    file_name,
)
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.states import TaskState
from app.infrastructure.external.knowbase_client import KnowbaseRefused

MD = "# 烟测\n\n回答在这里。\n"


class Always:
    """代理说成稿在（验收时记下路径）：脚本化的 app-server 不发 fileChange。"""

    async def exists(self, *, owner, directory, path):
        return True


class Reader:
    def __init__(self) -> None:
        self.text: str | None = MD
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def read(self, *, owner, directory, path):
        self.calls.append({"owner": owner, "directory": directory, "path": path})
        if self.error is not None:
            raise self.error
        return None if self.text is None else ReadFile(self.text, None)


class Area:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def grant_input_set(self, grant_id: str) -> str | None:
        return "f" * 64

    async def write_output(self, **kw: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.written.append(kw)
        return {"material_id": "m-out-1", "version_no": 1, "sha256": "a" * 64}


async def delivered(db) -> str:  # noqa: F811
    """烟测委托跑到交付，写了一份成稿；委托人登录过（auth_user 里有 subject）。"""
    async with db() as s:
        await s.execute(
            text(
                """insert into auth_user (id, issuer, subject, roles, scopes)
                   values (:i, 'https://casdoor.test', 'user-123', '[]', '[]')"""
            ),
            {"i": OWNER},
        )
        await s.commit()
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    try:
        runner, _, _, task_id = await seed(db, fake)
        runner.deliverables = Always()
        await drive(runner)
        for link in runner.links.values():
            if link.client:
                await link.client.close()
    finally:
        await fake.close()
    return task_id


async def rows(db) -> list[dict[str, Any]]:  # noqa: F811
    async with db() as s:
        found = await s.execute(text("select * from workbench_task_outputs"))
        return [dict(r) for r in found.mappings().all()]


async def test_a_delivered_draft_is_written_to_the_output_area(db):  # noqa: F811
    task_id = await delivered(db)
    async with db() as s:
        state = await s.execute(
            text("select state from workbench_tasks where id = :t"), {"t": task_id}
        )
        assert state.scalar_one() == TaskState.SUCCEEDED
    [row] = await rows(db)
    # 成稿在这次请求自己的那一格里（底稿/对话-…/…-机制烟测/烟测-回答.md）
    assert row["path"].startswith("底稿/") and row["path"].endswith("/烟测-回答.md")
    assert (row["status"], str(row["run_ref"])) == ("pending", task_id)
    reader, area = Reader(), Area()
    writer = OutputWriter(workbench_stores(db), reader, area)
    assert await writer.sweep() == 1
    assert reader.calls == [
        {
            "owner": OWNER,
            "directory": "/home/u/research/proj",
            "path": row["path"],
        }
    ]
    [written] = area.written
    assert written["beneficiary"] == "user-123"
    assert written["task_ref"] == f"knowbench:task:{task_id}"
    assert written["run_ref"] == task_id
    assert written["name"] == "烟测-回答.md"
    assert written["content"] == MD.encode("utf-8")
    # 内置的烟测不是请来的专家，不记专家；没钉数据，不记输入
    assert written["expert_id"] is None and written["input_set_id"] is None
    [row] = await rows(db)
    assert (row["status"], row["material_id"], row["version_no"]) == (
        "written",
        "m-out-1",
        1,
    )
    assert await writer.sweep() == 0  # 写过的不再写


async def test_not_written_does_not_block_and_is_tried_again(db):  # noqa: F811
    await delivered(db)
    reader, area = Reader(), Area()
    now = datetime.now(UTC)
    writer = OutputWriter(workbench_stores(db), reader, area, now=lambda: now)
    # 电脑不在线：记下原因，隔一阵再试
    reader.error = WorkbenchError("offline")
    reader.error.code = "agent_offline"
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["last_error"] == "成稿没读到：agent_offline"
    assert row["next_try_at"] == now + RETRY[0]
    assert await writer.sweep() == 0 and len(reader.calls) == 1  # 没到点不试
    # 到点了、knowbase 暂时不通：再记一次，间隔拉长
    reader.error = None
    area.error = ServiceUnavailableError("down", code="knowbase_unavailable")
    writer.now = lambda: now + RETRY[0]
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["attempts"]) == ("pending", 2)
    assert row["next_try_at"] == now + RETRY[0] + RETRY[1]
    # 再到点、通了：写进去
    area.error = None
    writer.now = lambda: now + RETRY[0] + RETRY[1]
    assert await writer.sweep() == 1
    [row] = await rows(db)
    assert row["status"] == "written" and row["last_error"] is None


async def test_what_knowbase_refuses_or_waits_too_long_stops_with_the_reason(db):  # noqa: F811
    await delivered(db)
    reader, area = Reader(), Area()
    writer = OutputWriter(workbench_stores(db), reader, area)
    area.error = KnowbaseRefused(413, "quota_exceeded")
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert row["status"] == "failed"
    assert row["last_error"] == "knowbase 不收：quota_exceeded"
    assert await writer.sweep() == 0  # 停下的不再试
    async with db() as s:
        await s.execute(
            text(
                "update workbench_task_outputs set status = 'pending', attempts = 3,"
                " last_error = null"
            )
        )
        await s.commit()
    reader.error = DeliverableTooLarge("too large")
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == ("failed", "deliverable_too_large")
    async with db() as s:
        await s.execute(
            text(
                "update workbench_task_outputs set status = 'pending', last_error = null"
            )
        )
        await s.commit()
    reader.error, reader.text = None, None  # 问不到那台电脑，已经过了 7 天
    writer.now = lambda: datetime.now(UTC) + GIVE_UP
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert row["status"] == "failed"
    assert row["last_error"] == "过了 7 天还没写进去：成稿没读到：问不到那台电脑"


def test_the_expert_is_recorded_only_for_a_summoned_knowbot() -> None:
    summoned = {
        "pack_source": "knowmart",
        "profile_id": "knowbot:0b8a6c3e-1f2d-4c5b-9a7e-6d5c4b3a2f10@3",
        "profile_version": "3",
    }
    assert expert_of(summoned) == ("0b8a6c3e-1f2d-4c5b-9a7e-6d5c4b3a2f10", "3")
    assert expert_of({"pack_source": "builtin", "profile_id": "SMOKE"}) == (None, None)
    assert file_name("底稿\\a\\成稿.md") == "成稿.md"
    assert file_name("成稿.md") == "成稿.md"
