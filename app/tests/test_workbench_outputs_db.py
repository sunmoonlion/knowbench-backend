"""委托交付的成稿替用户写进 knowbase 产出区（SDD 0043 第 2.6 节）：真数据库，用户电脑与 knowbase 用替身。

委托成功时每份成稿记一行待写（带验收认下时那份的 sha256）；runner 里那条线读用户电脑上的成稿
原样字节、以服务身份写进产出区，带出处与这件委托的授权 ref；交付后改过的不写；写不进去不挡交付，
电脑不在线、knowbase 不通、限流就隔一阵再试，明确的内容问题、文件没了、会话没了就停下、原因留着。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from test_deliverable_check import FakeService, stores_with
from test_workbench_advisor_db import ANSWER, PLAN, ScriptedAppServer, drive, seed
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import make_client as make_client  # noqa: F401

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.workbench.deliverable_check import (
    AgentDeliverableCheck,
    DeliverableTooLarge,
    RawFile,
)
from app.application.workbench.outputs import (
    GIVE_UP,
    RETRY,
    OutputWriter,
    expert_of,
    output_name,
)
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import NotFound, WorkbenchError
from app.domain.workbench.states import TaskState
from app.infrastructure.external.knowbase_client import KnowbaseRefused
from app.infrastructure.workbench.repository import WorkbenchRepository

MD = "# 烟测\n\n回答在这里。\n".encode()


class Always:
    """代理说成稿在（验收时记下路径与那份的 sha256）：脚本化的 app-server 不发 fileChange。"""

    def __init__(self, content: bytes | None = MD) -> None:
        self.content = content

    async def exists(self, *, owner, directory, path):
        return True

    async def digest(self, *, owner, directory, path):
        if self.content is None:
            return None  # 问不到（老的电脑、离线）
        return hashlib.sha256(self.content).hexdigest()


class Reader:
    def __init__(self, content: bytes | None = MD, mtime_ms: int | None = None) -> None:
        self.content = content
        self.mtime_ms = mtime_ms
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def read_bytes(self, *, owner, directory, path):
        self.calls.append({"owner": owner, "directory": directory, "path": path})
        if self.error is not None:
            raise self.error
        return None if self.content is None else RawFile(self.content, self.mtime_ms)


class Area:
    """knowbase 产出区的替身：照新的 knowbase 回显认下的出处依据（provenance_basis）。"""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.looked_up: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.echo = True  # False：老的 knowbase，不认出处依据、不回显
        self.earlier_sha = "b" * 64
        self.window_off = False  # 回显的委托起止对不上

    async def write_output(self, **kw: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.written.append(kw)
        out = {"material_id": "m-out-1", "version_no": 1, "sha256": "a" * 64}
        if self.echo:
            out["provenance_basis"] = {
                **({"grant_ref": kw["grant_ref"]} if kw.get("grant_ref") else {}),
                "task_window": {
                    "started_at": kw["task_started_at"],
                    "finished_at": kw["task_finished_at"],
                },
            }
            if self.window_off:
                out["provenance_basis"]["task_window"]["finished_at"] = None
        return out

    async def find_output(self, **kw: Any) -> dict[str, Any]:
        self.looked_up.append(kw)
        return {"material_id": "m-earlier", "version_no": 1, "sha256": self.earlier_sha}


def agent_refused(code: str) -> WorkbenchError:
    error = WorkbenchError(code)
    error.code = code
    return error


async def delivered(db, accepted: bytes | None = MD) -> str:  # noqa: F811
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
        runner.deliverables = Always(accepted)
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
    assert row["accepted_sha256"] == hashlib.sha256(MD).hexdigest()
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
    # 名字带上相对路径（段与段之间用全角斜杠）
    assert written["name"] == output_name(row["path"])
    assert written["name"].startswith("底稿／") and written["name"].endswith(
        "／烟测-回答.md"
    )
    assert written["content"] == MD
    # 内置的烟测不是请来的专家，不记专家；没钉数据，明说没用数据、不带授权 ref
    assert written["expert_id"] is None
    assert written["grant_ref"] is None and "no_inputs" not in written
    # 委托的起止（没有 ref 也带）：knowbase 把这段时间里会话授权读过的也算进上限
    started = datetime.fromisoformat(written["task_started_at"])
    finished = datetime.fromisoformat(written["task_finished_at"])
    assert started <= finished and finished >= row["created_at"]
    assert written["incomplete"] is False
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
    reader.error = agent_refused("agent_offline")
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


@pytest.mark.parametrize("accepted", [MD, None])
async def test_partial_read_retries_before_archiving_the_complete_report(db, accepted):  # noqa: F811
    await delivered(db, accepted=accepted)
    now = datetime.now(UTC)
    service = FakeService(
        {
            "content": base64.b64encode(MD[:5]).decode(),
            "size": len(MD),
            "mtime": int(now.timestamp() * 1000),
            "eof": False,
        }
    )
    reader = AgentDeliverableCheck(service, stores_with({"relay_user": "u"}))
    area = Area()
    writer = OutputWriter(workbench_stores(db), reader, area, now=lambda: now)
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert row["status"] == "pending"
    assert row["last_error"] == "成稿没读到：agent_service_failed"
    assert area.written == []

    service.result.update(content=base64.b64encode(MD).decode(), eof=True)
    writer.now = lambda: now + RETRY[0]
    assert await writer.sweep() == 1
    [written] = area.written
    assert written["content"] == MD
    [row] = await rows(db)
    assert row["status"] == "written" and row["last_error"] is None


async def test_what_knowbase_refuses_or_waits_too_long_stops_with_the_reason(db):  # noqa: F811
    await delivered(db)
    reader, area = Reader(), Area()
    writer = OutputWriter(workbench_stores(db), reader, area)
    area.error = KnowbaseRefused(415, "type_not_allowed")
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert row["status"] == "failed"
    assert row["last_error"] == "knowbase 不收：type_not_allowed"
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
    reader.error, reader.content = None, None  # 问不到那台电脑，已经过了 7 天
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
    # 名字带上相对路径（同一次交付不同目录的同名成稿各是各的），超长截掉、加短哈希
    assert output_name("底稿\\a\\成稿.md") == "底稿／a／成稿.md"
    assert output_name("成稿.md") == "成稿.md"
    long = output_name("底稿/" + "很长的目录" * 60 + "/成稿.md")
    assert len(long) == 200 and long.endswith(".md") and "~" in long
    assert long != output_name("底稿/" + "很长的目录" * 60 + "/成稿2.md")


async def reset(db) -> None:  # noqa: F811
    async with db() as s:
        await s.execute(
            text(
                "update workbench_task_outputs set status = 'pending', attempts = 0,"
                " last_error = null, next_try_at = now()"
            )
        )
        await s.commit()


async def test_binary_and_non_utf8_drafts_go_byte_for_byte(db):  # noqa: F811
    """独立审 10-30 第 2 条：成稿按原样的字节上传，不转文字；不是 UTF-8 的文本交 knowbase 判。"""
    raw = b"%PDF-1.7\n\xff\xfe\x00\x81"
    await delivered(db, accepted=raw)
    reader, area = Reader(raw), Area()
    assert await OutputWriter(workbench_stores(db), reader, area).sweep() == 1
    assert area.written[0]["content"] == raw


async def test_a_draft_changed_after_delivery_is_not_written(db):  # noqa: F811
    """读回来的和交付时取的指纹对不上（交付后改过）就不写，原因留着（0043 第 2.6 节出处照实）。"""
    await delivered(db)
    reader, area = Reader(MD + "\n我自己加的一段\n".encode()), Area()
    writer = OutputWriter(workbench_stores(db), reader, area)
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert row["status"] == "failed" and area.written == []
    assert row["last_error"] == "成稿交付后改过，和交付时的那份对不上，没写"


async def test_without_a_fingerprint_the_modified_time_decides(db, make_client):  # noqa: F811
    """独立审 10-30 第二轮第 9 条：交付时没取到指纹的，link 报的修改时间不晚于交付才认；
    晚于交付、或报不出来，不写，原因留着、委托页看得到。"""
    task_id = await delivered(db, accepted=None)
    [row] = await rows(db)
    assert row["accepted_sha256"] is None
    delivered_ms = int(row["created_at"].timestamp() * 1000)
    reader, area = Reader(MD, mtime_ms=delivered_ms + 3 * 60_000), Area()
    writer = OutputWriter(workbench_stores(db), reader, area)
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == (
        "failed",
        "按修改时间推断：成稿在交付之后改过（交付时没取到指纹，修改时间晚于交付），没写",
    )
    await reset(db)
    reader.mtime_ms = None
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert row["status"] == "failed" and row["last_error"].startswith("交付时没取到")
    async with make_client(OWNER) as http:
        shown = (await http.get(f"/api/workbench/tasks/{task_id}/data")).json()
    assert shown["outputs_not_saved"] == [
        {"path": row["path"], "reason": row["last_error"]}
    ]
    await reset(db)
    # 修改时间比交付晚一点点（2 分钟以内）：两边的钟、写完到记账的间隔，照认
    reader.mtime_ms = delivered_ms + 60_000
    assert await writer.sweep() == 1 and area.written[0]["content"] == MD


async def test_an_old_knowbase_that_does_not_echo_the_basis_is_not_written(db):  # noqa: F811
    """第二轮第 1 条：knowbase 没回显认下的出处依据（老的不认这几个参数、缺省不设上限）：
    当没写成，照退避再试，不记已写。"""
    await delivered(db)
    area = Area()
    area.echo = False
    assert await OutputWriter(workbench_stores(db), Reader(), area).sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["last_error"] == "knowbase 没回显认下的出处依据"


async def test_an_output_already_there_counts_only_if_it_is_the_delivered_one(db):  # noqa: F811
    """第二轮第 4 条：409 output_exists 说明那份内容不同：查回来，sha256 等于交付时的才记
    已写，不是就停下。"""
    task_id = await delivered(db)
    reader, area = Reader(), Area()
    area.error = KnowbaseRefused(409, "output_exists")
    writer = OutputWriter(workbench_stores(db), reader, area)
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == (
        "failed",
        "knowbase 里这次交付同名的是另一份，不是交付的那份",
    )
    [looked] = area.looked_up
    assert looked["task_ref"] == f"knowbench:task:{task_id}"
    assert looked["run_ref"] == task_id and looked["name"].endswith("／烟测-回答.md")
    await reset(db)
    area.earlier_sha = hashlib.sha256(MD).hexdigest()
    assert await writer.sweep() == 1
    [row] = await rows(db)
    assert (row["status"], row["material_id"]) == ("written", "m-earlier")


async def test_a_full_quota_is_tried_again_later(db):  # noqa: F811
    """小项：空间满了可重试（用户可能腾出地方），隔最长的那一档。"""
    await delivered(db)
    area = Area()
    area.error = KnowbaseRefused(413, "quota_exceeded")
    now = datetime.now(UTC)
    writer = OutputWriter(workbench_stores(db), Reader(), area, now=lambda: now)
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["next_try_at"]) == ("pending", now + RETRY[-1])


@pytest.mark.parametrize("status", [401, 403, 404, 429])
async def test_auth_missing_and_rate_limits_are_tried_again(db, status):  # noqa: F811
    """第 4 条：只有明确的内容问题停下；令牌、权限、找不到、限流照退避重试。"""
    await delivered(db)
    area = Area()
    area.error = KnowbaseRefused(status, "whatever")
    assert await OutputWriter(workbench_stores(db), Reader(), area).sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["attempts"]) == ("pending", 1)


@pytest.mark.parametrize(
    "code",
    [
        "type_not_allowed",
        "content_mismatch",
        "not_utf8",
        "too_large",
        "name_invalid",
        "empty",
    ],
)
async def test_content_problems_stop_at_once(db, code):  # noqa: F811
    await delivered(db)
    area = Area()
    area.error = KnowbaseRefused(422 if code != "too_large" else 413, code)
    assert await OutputWriter(workbench_stores(db), Reader(), area).sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == ("failed", f"knowbase 不收：{code}")


async def test_a_gone_file_or_session_stops_at_once(db):  # noqa: F811
    """文件没了、会话没了，停在 failed，不白试 7 天；不在白名单里的（用户可能把目录加回来）
    照重试，7 天到期再停（独立审 10-30 第三轮第 5 条）。"""
    await delivered(db)
    reader, area = Reader(), Area()
    writer = OutputWriter(workbench_stores(db), reader, area)
    reader.error = agent_refused("not_found")
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == ("failed", "成稿读不到了：not_found")
    await reset(db)
    reader.error = agent_refused("outside_whitelist")
    assert await writer.sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == (
        "pending",
        "成稿没读到：outside_whitelist",
    )

    class NoSession(WorkbenchRepository):
        async def get_session(self, session_id, **kw):  # type: ignore[override]
            raise NotFound("session not found")

    @contextlib.asynccontextmanager
    async def stores():  # type: ignore[no-untyped-def]
        async with db() as s:
            yield NoSession(s)

    await reset(db)
    reader.error = None
    assert await OutputWriter(stores, reader, area).sweep() == 0  # type: ignore[arg-type]
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == ("failed", "这件委托的会话没了")


async def test_only_succeeded_tasks_are_written(db):  # noqa: F811
    """第 6 条：交付那一行先记、委托后转成功；没成功（或之后改了状态）的不写。"""
    task_id = await delivered(db)
    async with db() as s:
        await s.execute(
            text("update workbench_tasks set state = 'FAILED' where id = :t"),
            {"t": task_id},
        )
        await s.commit()
    area = Area()
    assert await OutputWriter(workbench_stores(db), Reader(), area).sweep() == 0
    assert area.written == []
    [row] = await rows(db)
    assert (row["status"], row["attempts"]) == ("pending", 0)


@pytest.mark.parametrize(
    ("gaps", "incomplete"),
    [
        ([{"item": "上一年比较：你没要", "required": False}], False),
        ([{"item": "最近一年的年报：没取到", "required": True}], True),
        ([], False),
    ],
)
async def test_a_listed_task_carries_its_grant_ref_and_the_incomplete_mark(
    db,  # noqa: F811
    gaps,
    incomplete,
):
    """第二轮第 6、7 条：有清单的一律带这件委托的运行授权 ref（钉没钉数据都带，knowbase
    按 ref 下的授权与读数算上限）；只有必需的缺了、用户照做才标「数据不全」，可选的不算。"""
    task_id = await delivered(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.insert_task_resolution(
                {
                    "task_id": task_id,
                    "resolution_id": "res-1",
                    "revision": 2,
                    "consumer": "expert:k@1",
                    "params": {},
                    "grant_ref": "knowbench:run:out-1",
                    "status": "ready",
                    "detail": {"incomplete": [g["item"] for g in gaps], "gaps": gaps},
                }
            )
    area = Area()
    assert await OutputWriter(workbench_stores(db), Reader(), area).sweep() == 1
    [written] = area.written
    assert written["grant_ref"] == "knowbench:run:out-1"
    assert written["incomplete"] is incomplete


async def test_the_echoed_task_window_must_match(db):  # noqa: F811
    """第三轮第 1 条：回显里委托起止对不上（knowbase 没认这两个参数）也当没写成、照退避重试。"""
    await delivered(db)
    area = Area()
    area.window_off = True
    assert await OutputWriter(workbench_stores(db), Reader(), area).sweep() == 0
    [row] = await rows(db)
    assert (row["status"], row["last_error"]) == (
        "pending",
        "knowbase 没回显认下的出处依据",
    )


async def test_a_name_clash_within_one_delivery_says_so(db):  # noqa: F811
    """第三轮第 3 条：409 查回的那份正是这次交付另一份成稿的指纹：写明是重名。"""
    task_id = await delivered(db)
    other = "另一份成稿".encode()
    async with db() as s:
        await s.execute(
            text(
                """insert into workbench_task_outputs
                   (task_id, run_ref, path, accepted_sha256, status, material_id)
                   values (:t, :r, '别处/烟测-回答.md', :h, 'written', 'm-x')"""
            ),
            {"t": task_id, "r": task_id, "h": hashlib.sha256(other).hexdigest()},
        )
        await s.commit()
    area = Area()
    area.error = KnowbaseRefused(409, "output_exists")
    area.earlier_sha = hashlib.sha256(other).hexdigest()
    assert await OutputWriter(workbench_stores(db), Reader(), area).sweep() == 0
    found = {r["path"]: r for r in await rows(db)}
    mine = next(r for p, r in found.items() if not p.startswith("别处"))
    assert (mine["status"], mine["last_error"]) == (
        "failed",
        "和这次交付的另一份成稿重名，knowbase 里那个名字已经是那一份",
    )


def test_writing_waits_for_the_switch() -> None:
    """第三轮第 1 条：开关 WORKBENCH_OUTPUT_WRITE_ENABLED 默认关，关着没有写产出这条线
    （待写的留着，不推进、不计失败）；knowbase 认出处依据的那一版上线后再开。"""
    from app.bootstrap.workbench import build_output_writer
    from core.config import Settings

    assert Settings().workbench_output_write_enabled is False
    assert build_output_writer(None, Settings()) is None  # type: ignore[arg-type]
