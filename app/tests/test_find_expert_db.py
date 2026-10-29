"""AI 请专家（请专家方案设计第 4.9 节；decisions 10-29「AI 请专家」）：「找专家」那一轮在用户的项目
里用他自己的模型跑，交回按目录核过再给页面；summon 的要真不用另付钱；都不合适时挂需求、孵化
请求（附直接答的回答）；对话记来源 direct_answer。真 PostgreSQL。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text
from test_knowbot_test_runs_db import (
    AUTHOR,
    ENV,
    WORKSPACE,
    runner_for,
    setup_test_computer,
)
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_ledger_db import OWNER, ROOT, migrate
from test_workbench_ledger_db import db as db

from app.application.workbench.find_expert import FindExpertService, find_expert
from app.application.workbench.session_service import SessionService
from app.domain.workbench.packs import FIND_EXPERT
from app.infrastructure.workbench.repository import WorkbenchRepository

QUESTION = "每季度按固定口径对比几家电池公司的毛利率并预测下季"
FREE = {
    "knowbot_id": "k-free",
    "name": "财务分析与预测",
    "tagline": "看清走势与结构",
    "version": "2",
    "scope": {"does": "A 股非金融公司的财务分析", "does_not": "银行", "elsewhere": ""},
    "price": None,
    "free": True,
    "owned": True,
}
PAID = FREE | {
    "knowbot_id": "k-paid",
    "name": "毛利率对比",
    "price": "9.90",
    "free": False,
}


class Directory:
    def __init__(self, experts: list[dict[str, Any]]) -> None:
        self.list = experts
        self.asked: list[str] = []

    def configured(self) -> bool:
        return True

    async def experts(self, owner_subject: str) -> list[dict[str, Any]]:
        self.asked.append(owner_subject)
        return self.list


class Board:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(
        self, owner_subject: str, demand: dict[str, Any]
    ) -> dict[str, Any]:
        self.published.append((owner_subject, demand))
        return {"demand_id": "d1", "title": demand["title"]}


class Desk:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(request)
        return {"request_id": "r1", "created": True}


async def _project(db) -> str:  # type: ignore[no-untyped-def]
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            return await repo.create_project(
                owner_actor_id=OWNER,
                environment_id=ENV,
                workspace_root=WORKSPACE,
                path="finance",
                title="finance",
            )


async def _round(db, reply: dict[str, Any], directory: Directory):  # type: ignore[no-untyped-def]
    fake = ScriptedAppServer([json.dumps(reply, ensure_ascii=False)])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)
        async with db() as s:
            svc = FindExpertService(WorkbenchRepository(s), directory=directory)
            made = await svc.start(
                owner_actor_id=OWNER,
                project_id=project,
                question=QUESTION,
                idempotency_key="find-0001",
            )
            pending = await svc.result(OWNER, made["task_id"])
            assert pending["state"] != "SUCCEEDED" and "outcome" not in pending
        await drive(runner)
        [turn] = fake.turn_inputs
        assert QUESTION in turn
        assert AUTHOR not in turn  # 用户是谁不给模型
        return made, project
    finally:
        await close(runner)
        await fake.close()


async def test_a_free_fitting_expert_is_summoned_and_checked_against_the_directory(
    db,  # noqa: F811
) -> None:
    directory = Directory([FREE, PAID])
    reply = {
        "outcome": "summon",
        "picks": [
            {"knowbot_id": "k-free", "version": "2", "reason": "落在它的「做什么」里"}
        ],
    }
    made, project = await _round(db, reply, directory)
    async with db() as s:
        repo = WorkbenchRepository(s)
        task = await repo.get_task(made["task_id"], owner_actor_id=OWNER)
        assert task["profile_id"] == FIND_EXPERT.profile_id
        assert str(task["project_id"]) == project
        found = await FindExpertService(repo, directory=directory).result(
            OWNER, made["task_id"]
        )
    assert (found["state"], found["outcome"]) == ("SUCCEEDED", "summon")
    assert found["question"] == QUESTION
    [pick] = found["picks"]
    assert (pick["knowbot_id"], pick["name"], pick["free"]) == (
        "k-free",
        "财务分析与预测",
        True,
    )
    assert directory.asked == [AUTHOR]


async def test_a_paid_expert_called_summon_falls_back_to_candidates(db) -> None:  # noqa: F811
    directory = Directory([FREE, PAID])
    reply = {
        "outcome": "summon",
        "picks": [{"knowbot_id": "k-paid", "version": "2", "reason": "对比毛利率"}],
    }
    made, _ = await _round(db, reply, directory)
    async with db() as s:
        found = await FindExpertService(
            WorkbenchRepository(s), directory=directory
        ).result(OWNER, made["task_id"])
    assert found["outcome"] == "candidates"  # 花钱的由用户定
    assert [p["price"] for p in found["picks"]] == ["9.90"]


async def test_none_publishes_a_demand_and_asks_for_incubation_with_the_answer(
    db,  # noqa: F811
) -> None:
    directory = Directory([])
    reply = {
        "outcome": "none",
        "picks": [],
        "recommend": {"action": "incubate", "reason": "每季度都要按固定口径做"},
        "demand": {"title": "电池公司毛利率季度对比", "problem": QUESTION},
    }
    made, project = await _round(db, reply, directory)
    board, desk = Board(), Desk()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = FindExpertService(
            repo, directory=directory, demands=board, incubations=desk
        )
        found = await svc.result(OWNER, made["task_id"])
        assert (found["outcome"], found["recommend"]["action"]) == ("none", "incubate")
        assert found["demand"]["title"] == "电池公司毛利率季度对比"
        await svc.publish_demand(
            OWNER, made["task_id"], title="电池公司毛利率季度对比", problem=QUESTION
        )
        [(subject, demand)] = board.published
        assert subject == AUTHOR and demand["kind"] == "expert"
        assert date.fromisoformat(demand["deadline"]) >= date.today() + timedelta(
            days=29
        )
        # 先让自己的模型直接答过：那段对话记来源，回答附去作参考
        talk = await SessionService(repo).start(
            owner_actor_id=OWNER,
            kind="chat",
            project_id=project,
            title=QUESTION,
            origin="direct_answer",
        )
        async with repo.transaction():
            await repo.append_event(
                session_id=talk["session_id"],
                kind="item",
                event_type="item/completed",
                payload={
                    "item": {"type": "agentMessage", "text": "宁德时代毛利率约 22%"}
                },
            )
        session = await repo.get_session(talk["session_id"])
        assert session["origin"] == "direct_answer"
        await svc.ask_incubation(
            OWNER, made["task_id"], conversation_id=talk["session_id"]
        )
    [sent] = desk.sent
    assert (sent["owner_subject"], sent["question"], sent["project_id"]) == (
        AUTHOR,
        QUESTION,
        project,
    )
    assert sent["reference_answer"] == "宁德时代毛利率约 22%"
    assert sent["key"] == f"knowbench:find-expert:{made['task_id']}"


async def test_the_tool_lists_only_public_information() -> None:
    class Repo:
        async def subject_of_actor(self, actor: str) -> str:
            return AUTHOR

    got = await find_expert(Repo(), Directory([FREE]), {"owner_actor_id": OWNER})  # type: ignore[arg-type]
    [shown] = got["experts"]
    assert set(shown) == {
        "knowbot_id",
        "name",
        "tagline",
        "version",
        "scope",
        "price",
        "free",
    }
    assert (await find_expert(Repo(), None, {"owner_actor_id": OWNER}))["experts"] == []  # type: ignore[arg-type]


def test_the_migration_goes_down_and_up() -> None:
    """20261029_0032：对话加来源；downgrade 删列，再 upgrade 加回来。"""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set AGENT_TEST_DATABASE_URL to run PostgreSQL workbench tests")
    assert url.rsplit("/", 1)[-1].endswith("_tests")
    path = ROOT / "alembic/versions/20261029_0032_session_origin.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "20261028_0031"
    schema = "wb_test_" + uuid.uuid4().hex
    sync = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))

    def columns() -> set[str]:
        with sync.connect() as connection:
            return {
                c["name"]
                for c in inspect(connection).get_columns(
                    "workbench_sessions", schema=schema
                )
            }

    try:
        with sync.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            migrate(connection)
        assert "origin" in columns()
        for action, present in (("downgrade", False), ("upgrade", True)):
            with sync.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                with Operations.context(MigrationContext.configure(connection)):
                    getattr(module, action)()
            assert ("origin" in columns()) is present
    finally:
        with sync.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync.dispose()
