"""专家读本项目里别的对话与底稿（PRD/apps/investment.md 7.4；F-PROJ-06；AT-INV-09、10）。

真数据库；专家停在等用户决定的那一刻去读（那时方向盘在专家手里）。
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from test_fin_review_advisor_db import ALL, say
from test_fin_review_pack import PROFILE, SCOPE
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_review_db import unbalanced

from app.application.workbench.delegation import DelegationService
from app.application.workbench.ledger import Ledger
from app.application.workbench.project_service import ProjectService
from app.application.workbench.records import ProjectRecords
from app.application.workbench.session_service import SessionService
from app.bootstrap.workbench import build_runner
from app.domain.workbench.errors import NotFound, RecordsRefused
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")

WORKSPACE = "/home/u/research"
STRANGER = str(uuid.uuid4())


async def talk(repo, project_id, kind, words, owner=OWNER):
    """一段已经有几句话的对话（直接记账，不经沙箱）。"""
    sid = (
        await SessionService(repo).start(
            owner_actor_id=owner, kind=kind, project_id=project_id
        )
    )["session_id"]
    async with repo.transaction():
        for n, (asked, answered) in enumerate(words):
            await repo.append_event(
                session_id=sid,
                kind="turn",
                event_type="turn/requested",
                payload={"by": "user", "text": asked, "request_id": f"r{n}"},
            )
            await repo.append_event(
                session_id=sid,
                kind="item",
                event_type="item/completed",
                payload={
                    "turnId": f"t{n}",
                    "item": {"type": "agentMessage", "text": answered},
                },
            )
            await repo.append_event(
                session_id=sid,
                kind="turn",
                event_type="turn/completed",
                payload={"turn": {"id": f"t{n}", "status": "completed"}},
            )
    return sid


async def world(db, replies, *, records=False):
    """两个项目。恒瑞项目里有一段聊天、一段工作，专家在第三段对话里做财报体检，停在勾稽。"""
    fake = ScriptedAppServer([say(r) for r in replies])
    await fake.start()
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            env = await repo.register_environment(
                owner_actor_id=OWNER,
                name="pc",
                agent_version="0.1.0",
                codex_version="0.155.1",
                roots=[WORKSPACE],
                ceiling={},
            )
            await repo.register_sandbox(
                owner_actor_id=OWNER,
                app_server_url=f"ws://127.0.0.1:{fake.port}",
                token_ref="inline:t",
                codex_version="0.155.1",
            )
        projects = ProjectService(repo)
        hengrui = await projects.create(
            owner_actor_id=OWNER,
            environment_id=env,
            workspace_root=WORKSPACE,
            path="hengrui",
        )
        other = await projects.create(
            owner_actor_id=OWNER,
            environment_id=env,
            workspace_root=WORKSPACE,
            path="maotai",
        )
        chat = await talk(
            repo,
            hengrui["id"],
            "chat",
            [("毛利率是什么", "毛利率是……"), ("再说说净利率", "净利率是……")],
        )
        work = await talk(
            repo, hengrui["id"], "work", [("整理年报要点", "已整理到 notes.md")]
        )
        elsewhere = await talk(repo, other["id"], "chat", [("茅台的事", "……")])
        made = await DelegationService(repo).from_expert_entry(
            owner_actor_id=OWNER,
            project_id=hengrui["id"],
            profile_id="FIN_REVIEW",
            question="恒瑞医药 2023 到 2025 年的盈利能力怎么样？",
            budget_limit=Decimal("2"),
            idempotency_key="k-records-0001",
        )
    runner = build_runner(
        db,
        publisher=RedisPublisher(None, "t"),
        runner_id="r",
        poll_seconds=0.05,
        records=records,
    )
    await drive(runner)
    return (
        fake,
        runner,
        {
            "task": made["task_id"],
            "current": made["session_id"],
            "chat": chat,
            "work": work,
            "elsewhere": elsewhere,
            "project": hengrui["id"],
            "other_project": other["id"],
        },
    )


async def read(db, call, *args, owner=OWNER):
    async with db() as s:
        records = ProjectRecords(WorkbenchRepository(s), owner_actor_id=owner)
        return await getattr(records, call)(*args)


async def test_the_expert_reads_the_project_it_works_in(db):
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        listed = (await read(db, "list_conversations", ids["task"]))["conversations"]
        # AT-INV-09：只列出本项目的对话
        assert [c["conversation"] for c in listed] == [
            ids["chat"],
            ids["work"],
            ids["current"],
        ]
        assert [(c["kind"], c["turns"], c["current"]) for c in listed] == [
            ("chat", 2, False),
            ("work", 1, False),
            ("expert", 3, True),
        ]
        assert listed[0]["first_message"] == "毛利率是什么"
        assert listed[0]["title"] is None or isinstance(listed[0]["title"], str)

        got = await read(db, "read_conversation", ids["task"], ids["chat"])
        assert (got["page"], got["pages"], got["kind"]) == (1, 1, "chat")
        assert [(e["who"], e["said"]) for e in got["entries"]] == [
            ("user", "毛利率是什么"),
            ("assistant", "毛利率是……"),
            ("user", "再说说净利率"),
            ("assistant", "净利率是……"),
        ]

        # 读了什么，账里有
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=ids["current"])
        noted = [e["payload"] for e in events if e["type"] == "records/read"]
        assert [n["tool"] for n in noted] == [
            "list_project_conversations",
            "read_project_conversation",
        ]
        assert noted[1]["conversation"] == ids["chat"]
        assert all(
            e["task_id"] == ids["task"] for e in events if e["type"] == "records/read"
        )
    finally:
        await close(runner)
        await fake.close()


async def test_nothing_outside_the_project_can_be_read(db):
    """AT-INV-10"""
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        with pytest.raises(NotFound):
            await read(db, "read_conversation", ids["task"], ids["elsewhere"])
        with pytest.raises(NotFound):
            await read(db, "read_conversation", ids["task"], str(uuid.uuid4()))
        # 编号不是编号的样子：按找不到答，不走到数据库里去
        for junk in ("", "../etc/passwd", "1 or 1=1", None, 7):
            with pytest.raises(NotFound):
                await read(db, "read_conversation", ids["task"], junk)
            with pytest.raises(NotFound):
                await read(db, "list_conversations", junk)
        # 当前这段对话不经工具读
        with pytest.raises(RecordsRefused):
            await read(db, "read_conversation", ids["task"], ids["current"])
        # 别的用户拿着这个委托的编号：和不存在一样
        for call, args in (
            ("list_conversations", (ids["task"],)),
            ("read_conversation", (ids["task"], ids["chat"])),
        ):
            with pytest.raises(NotFound):
                await read(db, call, *args, owner=STRANGER)
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=ids["current"])
        assert not [e for e in events if e["type"] == "records/read"]
    finally:
        await close(runner)
        await fake.close()


async def test_only_while_the_expert_is_working(db):
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        assert await read(db, "list_conversations", ids["task"])
        async with db() as s:
            repo = WorkbenchRepository(s)
            (pending,) = await repo.list_interactions(
                session_id=ids["current"], status="pending"
            )
            token = next(
                e["payload"]["token"]
                for e in await repo.list_events(session_id=ids["current"])
                if e["type"] == "interaction/opened"
            )
            await Ledger(repo).respond_interaction(
                str(pending["id"]),
                token=token,
                response={"decision": "stop"},
                owner_actor_id=OWNER,
            )
        # 委托结束、方向盘交回之后：两个工具都拒绝
        for call, args in (
            ("list_conversations", (ids["task"],)),
            ("read_conversation", (ids["task"], ids["chat"])),
        ):
            with pytest.raises(RecordsRefused):
                await read(db, call, *args)
    finally:
        await close(runner)
        await fake.close()


async def test_the_step_tells_the_model_about_the_tools_only_when_switched_on(db):
    fake, runner, ids = await world(db, ALL[:1] + [PROFILE, unbalanced()], records=True)
    try:
        assert len(fake.turn_inputs) == 3
        for text in fake.turn_inputs:
            assert "## Records of this project" in text
            assert f'task="{ids["task"]}"' in text
            assert "read_project_conversation" in text
    finally:
        await close(runner)
        await fake.close()


async def test_the_step_says_nothing_about_the_tools_when_switched_off(db):
    fake, runner, _ = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        assert len(fake.turn_inputs) == 3
        assert not any("Records of this project" in t for t in fake.turn_inputs)
    finally:
        await close(runner)
        await fake.close()


async def test_a_long_conversation_is_read_page_by_page(db):
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            long = await talk(
                repo,
                ids["project"],
                "chat",
                [(f"问 {n}", f"答 {n}") for n in range(45)],
            )
        first = await read(db, "read_conversation", ids["task"], long, 1)
        assert (first["page"], first["pages"], len(first["entries"])) == (1, 3, 40)
        third = await read(db, "read_conversation", ids["task"], long, 3)
        assert [e["said"] for e in third["entries"]][-1] == "答 44"
        assert len(json.dumps(first, default=str)) < 40 * 2200
    finally:
        await close(runner)
        await fake.close()
