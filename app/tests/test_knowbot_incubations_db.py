"""作为运行方跑孵化的一轮（专家孵化方案第八节）：真数据库、真 runner、假 app-server 按脚本回复。

受理 → 推进器在作者的电脑上建「孵化」项目、开对话、用孵化方法建委托 → runner 驱动 → 收交回物。
都在作者的账号下跑（decisions ⑯）；交给模型的只有契约列死的材料（0029 第 6.4 节）。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, text
from test_knowbot_test_runs_db import (
    AUTHOR,
    ENV,
    WORKSPACE,
    Clock,
    runner_for,
    setup_test_computer,
)
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_ledger_db import OWNER, ROOT, migrate
from test_workbench_ledger_db import db as db

from app.application.workbench.incubations import (
    FOLDER,
    IncubationConductor,
    IncubationService,
)
from app.application.workbench.test_folders import FolderRefused
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import (
    IncubationConflict,
    IncubationNoComputer,
    NotFound,
)
from app.domain.workbench.incubations import IncubationRequest
from app.domain.workbench.packs import INCUBATE
from app.infrastructure.workbench.repository import WorkbenchRepository

SEED = "我想知道这家公司未来三年的收入会怎样"
HANDBACK = {
    "sources": [
        {"path": "name", "kind": "idea", "quote": "未来三年的收入"},
        {"path": "steps/scan/title", "kind": "proposal"},
        {"path": "steps/scan/does", "kind": "proposal"},
    ],
    "gaps": [
        {
            "id": "g1",
            "question": "收入用合并还是母公司口径？",
            "proposal": "合并口径",
            "options": ["合并", "母公司"],
        }
    ],
    "suggestions": [
        {
            "id": "s1",
            "path": "name",
            "op": "set",
            "value": "未来三年的收入",
            "reason": "创意问的就是这个",
        },
        {
            "id": "s2",
            "path": "steps/scan",
            "op": "add",
            "value": {"title": "数据摸底", "does": "看清有哪些表、哪些年份"},
            "reason": "先看清数据才能往下做",
        },
    ],
    "self_check": [{"suggestion_id": "s0", "turned_into_gap": "g1"}],
}
GOOD = json.dumps(HANDBACK, ensure_ascii=False)


def request(
    incubation_id: str = "knowlab-inc-0001",
    *,
    action: str = "draft",
    budget: str = "10",
    environment_id: str = ENV,
    root: str = WORKSPACE,
    timeout: int = 3600,
) -> IncubationRequest:
    body = {
        "contract": "knowbot-incubation/v1",
        "incubation_id": incubation_id,
        "owner_subject": AUTHOR,
        "action": action,
        "seed": {"kind": "question", "text": SEED},
        "workdraft": {
            "body": {"name": "收入预测", "steps": []},
            "sources": {"name": {"kind": "user_text", "ref": "record:r1"}},
        },
        "answers": [
            {"id": "q-1", "question": "看几年？", "proposal": "三年", "answer": "三年"}
        ],
        "data_formats": [
            {
                "name": "利润表",
                "tables": [
                    {"name": "利润表", "columns": ["2024"], "rows": ["营业收入"]}
                ],
            }
        ],
        "budget": {"limit": budget},
        "timeout_seconds": timeout,
        "runner": {
            "kind": "user",
            "environment_id": environment_id,
            "workspace_root": root,
        },
    }
    if action == "revise":
        body["instruction"] = "第一步拆成两步"
    return IncubationRequest.model_validate(body)


async def accept(db, req):
    async with db() as s:
        return await IncubationService(WorkbenchRepository(s)).accept(
            req, caller="knowlab"
        )


async def status(db, incubation_id="knowlab-inc-0001"):
    async with db() as s:
        return await IncubationService(WorkbenchRepository(s)).status(incubation_id)


async def cancel(db, incubation_id="knowlab-inc-0001"):
    async with db() as s:
        return await IncubationService(WorkbenchRepository(s)).cancel(incubation_id)


async def the_task(db):
    async with db() as s:
        return (
            (
                await s.execute(
                    text(
                        """select t.id, t.state, t.profile_id, t.pack_source,
                                  t.owner_actor_id, t.budget, p.path, p.title,
                                  p.workspace_root
                           from workbench_tasks t
                           join workbench_projects p on p.id = t.project_id"""
                    )
                )
            )
            .mappings()
            .one()
        )


async def test_a_round_runs_in_the_authors_incubation_project_and_hands_back(db):
    fake = ScriptedAppServer([GOOD])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        async with db() as s:  # 作者的记忆不给孵化方法看（0029 第 6.4 节）
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.insert_memory(
                    owner_actor_id=OWNER,
                    kind="fact",
                    text="我持有这家公司",
                    source=None,
                )
        got = await accept(db, request())
        assert got["status"] == "queued" and got["handback"] is None
        assert await accept(db, request()) == got  # 同样的内容再交一次：回原状态
        conductor = IncubationConductor(workbench_stores(db))
        await conductor.tick()  # 建「孵化」项目、开对话、建委托、排驾驶
        running = await status(db)
        assert running["status"] == "running" and running["task_id"]
        task = await the_task(db)
        assert task["path"] == FOLDER and task["title"] == FOLDER
        assert task["workspace_root"] == WORKSPACE
        assert task["profile_id"] == INCUBATE.profile_id
        assert task["pack_source"] == "builtin"  # 内置的孵化方法，不是分身
        assert str(task["owner_actor_id"]) == OWNER  # 在作者的账号下
        assert Decimal(task["budget"]["limit"]) == Decimal("10")  # 作者设的上限
        await drive(runner)
        await conductor.tick()  # 收交回物
        done = await status(db)
        assert done["status"] == "succeeded", done
        assert done["handback"] == HANDBACK
        assert Decimal(done["cost"]["spent"]) > 0
        assert done["finished_at"] and done["error"] is None
        # 交给模型的只有契约列死的材料：创意、现稿、答复、数据的格式与方法
        [turn] = fake.turn_inputs
        assert SEED in turn and "营业收入" in turn and "看几年？" in turn
        assert "Investment judgments belong to the author" in turn
        for hidden in (AUTHOR, ENV, "knowlab-inc-0001", "我持有这家公司", "budget"):
            assert hidden not in turn, hidden
    finally:
        await close(runner)
        await fake.close()


async def test_the_same_id_with_other_content_is_refused(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request())
        with pytest.raises(IncubationConflict):
            await accept(db, request(budget="5"))
        with pytest.raises(NotFound):
            await status(db, "knowlab-inc-9999")
    finally:
        await fake.close()


async def test_a_computer_that_is_not_the_authors_is_refused(db):
    with pytest.raises(IncubationNoComputer):  # 没登录过工作台
        await accept(db, request())
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        for wrong in (
            request(environment_id="00000000-0000-4000-8000-000000000000"),
            request(root="/somewhere/else"),  # 根目录不在那台电脑的白名单里
        ):
            with pytest.raises(IncubationNoComputer) as caught:
                await accept(db, wrong)
            assert caught.value.code == "incubation_computer_unavailable"
    finally:
        await fake.close()


async def test_a_handback_that_fails_its_checks_is_redone_once(db):
    fake = ScriptedAppServer([json.dumps({"gaps": []}), GOOD])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request())
        conductor = IncubationConductor(workbench_stores(db))
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        done = await status(db)
        assert done["status"] == "succeeded", done
        assert len(fake.turn_inputs) == 2 and "rework #1" in fake.turn_inputs[1]
    finally:
        await close(runner)
        await fake.close()


async def test_a_handback_that_fails_twice_ends_the_round(db):
    bad = json.dumps(
        {"sources": [], "gaps": [], "suggestions": [{"id": "s1"}], "self_check": []}
    )
    fake = ScriptedAppServer([bad, bad])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request())
        conductor = IncubationConductor(workbench_stores(db))
        await conductor.tick()
        await drive(runner)
        await conductor.tick()  # 返工用完停下来等人：孵化里没人答，这一轮记失败
        done = await status(db)
        assert done["status"] == "failed", done
        assert "after one rework" in done["error"]
        assert done["handback"] is None
        assert (await the_task(db))["state"] == "CANCELLED"
    finally:
        await close(runner)
        await fake.close()


async def test_a_handback_off_the_contract_is_not_handed_on(db):
    """过了结构验收、但碰了工程层（「高级」）：照契约核不过，这一轮记失败，不交回。"""
    off = dict(HANDBACK)
    off["suggestions"] = [
        {
            "id": "s1",
            "path": "steps/scan/advanced",
            "op": "set",
            "value": {"tools": []},
            "reason": "x",
        }
    ]
    fake = ScriptedAppServer([json.dumps(off)])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request())
        conductor = IncubationConductor(workbench_stores(db))
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        done = await status(db)
        assert done["status"] == "failed" and done["handback"] is None
        assert "does not fit knowbot-incubation/v1" in done["error"]
    finally:
        await close(runner)
        await fake.close()


@pytest.mark.parametrize(("budget", "tokens"), [("0.01", 1000), ("0.06", 10000)])
async def test_the_round_stops_at_the_authors_limit(db, budget, tokens):
    """上限比一步的预留还少（开不了步），或这一轮花超了：停下，记 over_budget。"""
    fake = ScriptedAppServer([GOOD], tokens_per_turn=tokens)
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request(budget=budget))
        conductor = IncubationConductor(workbench_stores(db))
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        done = await status(db)
        assert done["status"] == "over_budget", done
        assert done["handback"] is None
        # 花超了的那一轮照实记花了多少（作者的钱）；开不了步的一分没花
        assert (Decimal(done["cost"]["spent"]) > 0) == (tokens > 1000)
        assert (await the_task(db))["state"] == "CANCELLED"
    finally:
        await close(runner)
        await fake.close()


async def test_cancel_stops_the_round_and_a_queued_one_never_starts(db):
    fake = ScriptedAppServer([GOOD])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request())
        conductor = IncubationConductor(workbench_stores(db))
        await conductor.tick()
        stopped = await cancel(db)
        assert stopped["status"] == "cancelled" and stopped["finished_at"]
        assert await cancel(db) == stopped  # 已经结束的回原状态
        async with db() as s:
            task = await WorkbenchRepository(s).get_task(stopped["task_id"])
        assert task["state"] == "CANCELLED"
        await conductor.tick()
        assert (await status(db))["status"] == "cancelled"
        # 还没开跑就取消：不会再开委托
        await accept(db, request("knowlab-inc-0002"))
        assert (await cancel(db, "knowlab-inc-0002"))["status"] == "cancelled"
        await conductor.tick()
        later = await status(db, "knowlab-inc-0002")
        assert later["status"] == "cancelled" and later["task_id"] is None
        with pytest.raises(NotFound):
            await cancel(db, "knowlab-inc-9999")
    finally:
        await fake.close()


async def test_one_round_at_a_time_on_one_computer_and_root(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-inc-0001"))
        await accept(db, request("knowlab-inc-0002"))
        await IncubationConductor(workbench_stores(db)).tick()
        assert (await status(db, "knowlab-inc-0001"))["status"] == "running"
        assert (await status(db, "knowlab-inc-0002"))["status"] == "queued"
    finally:
        await fake.close()


async def test_an_offline_computer_waits_then_gives_up(db):
    fake = ScriptedAppServer([])
    await fake.start()
    clock = Clock()
    try:
        await setup_test_computer(db, fake)
        async with db() as s:
            await WorkbenchRepository(s).set_environment_status(ENV, "offline")
            await s.commit()
        await accept(db, request(timeout=600))
        conductor = IncubationConductor(workbench_stores(db), now=clock)
        await conductor.tick()
        waiting = await status(db)
        assert waiting["status"] == "waiting", waiting
        assert waiting["waiting_for"] == "computer_offline"
        assert waiting["task_id"] is None
        clock.at += timedelta(seconds=601)
        await conductor.tick()
        done = await status(db)
        assert done["status"] == "failed" and "offline" in done["error"]
    finally:
        await fake.close()


class FakeFolders:
    def __init__(self, ready: bool = True, refuse: bool = False) -> None:
        self.ready = ready
        self.refuse = refuse
        self.calls: list[tuple[str, str, str]] = []

    async def ensure(self, *, owner: str, root: str, folder: str) -> bool:
        self.calls.append((owner, root, folder))
        if self.refuse:
            raise FolderRefused(f"{root}/{folder}: outside_whitelist")
        return self.ready


async def test_the_incubation_folder_is_made_on_the_authors_computer(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        folders = FakeFolders(ready=False)
        await accept(db, request("knowlab-inc-0001"))
        await IncubationConductor(workbench_stores(db), folders=folders).tick()
        assert folders.calls == [(OWNER, WORKSPACE, FOLDER)]
        waiting = await status(db, "knowlab-inc-0001")
        assert waiting["status"] == "waiting"
        assert waiting["waiting_for"] == "folder_not_ready"
        folders.ready = True
        await IncubationConductor(workbench_stores(db), folders=folders).tick()
        assert (await status(db, "knowlab-inc-0001"))["status"] == "running"
        # 本地 link 明确拒了：等下去也不会好，马上报错
        await accept(db, request("knowlab-inc-0002", root=WORKSPACE))
        await cancel(db, "knowlab-inc-0001")
        await IncubationConductor(
            workbench_stores(db), folders=FakeFolders(refuse=True)
        ).tick()
        refused = await status(db, "knowlab-inc-0002")
        assert refused["status"] == "failed"
        assert "refused to make the folder" in refused["error"]
    finally:
        await fake.close()


def test_the_migration_goes_down_and_up() -> None:
    """20261028_0031：建孵化一轮的表；downgrade 删表，再 upgrade 建回来。"""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set AGENT_TEST_DATABASE_URL to run PostgreSQL workbench tests")
    assert url.rsplit("/", 1)[-1].endswith("_tests")
    path = ROOT / "alembic/versions/20261028_0031_incubations.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "20261028_0030"
    schema = "wb_test_" + uuid.uuid4().hex
    sync = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))

    def tables() -> set[str]:
        with sync.connect() as connection:
            return set(inspect(connection).get_table_names(schema=schema))

    try:
        with sync.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            migrate(connection)
        assert "workbench_incubations" in tables()
        for action, present in (("downgrade", False), ("upgrade", True)):
            with sync.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                with Operations.context(MigrationContext.configure(connection)):
                    getattr(module, action)()
            assert ("workbench_incubations" in tables()) is present
    finally:
        with sync.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync.dispose()
