"""知识库第一期（SDD 0011；F-LIB-01/03/04/05/08，AT-LIB-01/03/04/05）：
底稿与交回物自动归入、读、改名、拿掉；网页与 MCP 两条路；别人看不到。真数据库。"""

from __future__ import annotations

import json
import uuid

import pytest
from test_fin_review_advisor_db import ALL
from test_fin_review_pack import METRICS, NOTE, PROFILE, SCOPE
from test_workbench_advisor_db import close
from test_workbench_ledger_db import OTHER, OWNER
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_mcp_routes import bearer, client, rpc, served, tool  # noqa: F401
from test_workbench_records_db import world
from test_workbench_review_db import unbalanced
from test_workbench_routes import make_client as make_client  # noqa: F401
from test_workbench_step_view_db import run

from app.application.workbench.library import Library
from app.domain.workbench.errors import NotFound
from app.domain.workbench.library import parse_item_id
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")

MCP = "/api/mcp/workbench"


async def library(db, owner=OWNER):
    async with db() as s:
        yield Library(WorkbenchRepository(s), owner_actor_id=owner)


async def finished(db):
    fake, runner, *_, task_id = await run(db, ALL)
    await close(runner)
    await fake.close()
    return task_id


def test_item_ids_are_strict():
    tid = str(uuid.uuid4())
    assert parse_item_id(f"dossier:{tid}") == ("dossier", tid, None)
    assert parse_item_id(f"deliverable:{tid}:metrics") == (
        "deliverable",
        tid,
        "metrics",
    )
    for bad in (
        None,
        7,
        "dossier",
        "dossier:nope",
        f"dossier:{tid}:x",
        f"deliverable:{tid}",
        f"deliverable:{tid}:bad name",
        f"upload:{tid}",
    ):
        assert parse_item_id(bad) is None


async def test_a_finished_review_shows_up_as_one_dossier_and_its_deliverables(db):
    task_id = await finished(db)
    async with db() as s:
        lib = Library(WorkbenchRepository(s), owner_actor_id=OWNER)
        items = await lib.listing()
        assert [i["kind"] for i in items].count("dossier") == 1
        dossier = next(i for i in items if i["kind"] == "dossier")
        assert dossier["id"] == f"dossier:{task_id}"
        assert dossier["title"].startswith("财报体检")
        assert dossier["versions"] == 1 and dossier["project_id"]
        deliverables = [i for i in items if i["kind"] == "deliverable"]
        assert {i["source"]["artifact"] for i in deliverables} >= {
            "profile",
            "metrics",
            "note",
        }
        metrics = next(i for i in deliverables if i["source"]["artifact"] == "metrics")
        assert metrics["title"].startswith("算指标")
        assert metrics["size_bytes"] > 0 and metrics["versions"] == 1
        # 只列底稿；按名字找
        assert [i["kind"] for i in await lib.listing(kind="dossier")] == ["dossier"]
        assert all("指标" in i["title"] for i in await lib.listing(q="指标"))
        # 别人什么都看不到（F-LIB-05）
        stranger = Library(WorkbenchRepository(s), owner_actor_id=OTHER)
        assert await stranger.listing() == []
        with pytest.raises(NotFound):
            await stranger.item(dossier["id"])


async def test_an_unfinished_delegation_is_not_in_the_library_yet(db):
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        async with db() as s:
            lib = Library(WorkbenchRepository(s), owner_actor_id=OWNER)
            assert await lib.listing() == []
    finally:
        await close(runner)
        await fake.close()


async def test_reading_gives_the_text_and_a_citation(db):
    task_id = await finished(db)
    async with db() as s:
        lib = Library(WorkbenchRepository(s), owner_actor_id=OWNER)
        paper = await lib.content(f"dossier:{task_id}")
        assert NOTE["answer"] in paper["text"] and paper["version"] == 1
        assert len(paper["sha256"]) == 64 and paper["content"] is None
        table = await lib.content(f"deliverable:{task_id}:metrics", 1)
        assert table["content"] == METRICS and table["version"] == 1
        assert table["sha256"] and json.loads(table["text"]) == METRICS
        with pytest.raises(NotFound):
            await lib.content(f"deliverable:{task_id}:metrics", 2)
        read = await lib.tool_read(f"deliverable:{task_id}:metrics")
        assert read["citation"] == {
            "library_item_id": f"deliverable:{task_id}:metrics",
            "version": 1,
            "sha256": table["sha256"],
        }
        assert read["pages"] == 1 and json.loads(read["text"]) == METRICS


async def test_the_user_can_rename_and_take_an_item_out(db):
    task_id = await finished(db)
    item_id = f"deliverable:{task_id}:metrics"
    async with db() as s:
        lib = Library(WorkbenchRepository(s), owner_actor_id=OWNER)
        renamed = await lib.rename(item_id, "  上海机场指标表 ")
        assert renamed["title"] == "上海机场指标表"
        assert renamed["default_title"].startswith("算指标")
        await lib.remove(item_id)
        assert item_id not in {i["id"] for i in await lib.listing()}
        gone = next(
            i for i in await lib.listing(include_deleted=True) if i["id"] == item_id
        )
        assert gone["deleted"] is True and gone["title"] == "上海机场指标表"
        with pytest.raises(NotFound):
            await lib.content(item_id)
        # 原委托的记录不动（删的是「从知识库拿掉」）
        repo = WorkbenchRepository(s)
        assert await repo.get_artifact(task_id=task_id, name="metrics") is not None


async def test_the_web_routes(db, make_client):  # noqa: F811
    task_id = await finished(db)
    as_user = make_client
    async with as_user(OWNER) as http:
        listed = await http.get("/api/workbench/library")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert any(i["id"] == f"dossier:{task_id}" for i in items)
        one = await http.get(f"/api/workbench/library/deliverable:{task_id}:metrics")
        assert one.status_code == 200
        assert one.json()["item"]["version_list"][0]["version"] == 1
        content = await http.get(
            f"/api/workbench/library/deliverable:{task_id}:metrics/versions/1/content"
        )
        assert content.status_code == 200 and content.json()["content"] == METRICS
        renamed = await http.patch(
            f"/api/workbench/library/deliverable:{task_id}:metrics",
            json={"title": "我的表"},
        )
        assert (
            renamed.status_code == 200 and renamed.json()["item"]["title"] == "我的表"
        )
        assert (
            await http.delete(f"/api/workbench/library/deliverable:{task_id}:metrics")
        ).status_code == 204
        assert (
            await http.get(f"/api/workbench/library/deliverable:{task_id}:metrics")
        ).status_code == 404
        assert (await http.get("/api/workbench/library/nonsense")).status_code == 404
    async with as_user(OTHER) as http:
        assert (await http.get("/api/workbench/library")).json()["items"] == []
        assert (
            await http.get(f"/api/workbench/library/dossier:{task_id}")
        ).status_code == 404


async def test_codex_reads_the_library_without_a_task(served, db):  # noqa: F811
    task_id = await finished(db)
    async with client(served) as http:
        listed = await http.post(
            MCP, json=tool("list_library", kind="dossier"), headers=bearer()
        )
        result = listed.json()["result"]
        assert result["isError"] is False
        items = result["structuredContent"]["items"]
        assert [i["item"] for i in items] == [f"dossier:{task_id}"]
        assert items[0]["expert"].startswith("财报体检")
        read = await http.post(
            MCP,
            json=tool("read_library_item", item=f"dossier:{task_id}"),
            headers=bearer(),
        )
        got = read.json()["result"]["structuredContent"]
        assert NOTE["answer"] in got["text"]
        assert got["citation"]["library_item_id"] == f"dossier:{task_id}"
        missing = await http.post(
            MCP,
            json=tool("read_library_item", item="dossier:" + str(uuid.uuid4())),
            headers=bearer(),
        )
        assert missing.json()["result"]["isError"] is True
        # 别人的令牌读不到
        other = await http.post(
            MCP, json=tool("list_library"), headers=bearer(owner=OTHER)
        )
        assert other.json()["result"]["structuredContent"]["items"] == []
        tools = await http.post(MCP, json=rpc("tools/list"), headers=bearer())
        names = [t["name"] for t in tools.json()["result"]["tools"]]
        assert "list_library" in names and "read_library_item" in names
