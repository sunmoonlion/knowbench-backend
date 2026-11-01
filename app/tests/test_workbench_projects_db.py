"""项目、对话的种类、两条规矩（一个项目一个专家；专家在时别的对话不工作）：真数据库。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from decimal import Decimal

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from test_workbench_ledger_db import OTHER, OWNER, ROOT, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.agent_service import AgentOffline, AgentRefused
from app.application.workbench.ledger import Ledger
from app.application.workbench.project_service import ProjectService
from app.application.workbench.session_service import SessionService
from app.domain.workbench.errors import (
    ConversationChangeRefused,
    NoSandbox,
    NotFound,
    ProjectArchived,
    ProjectBusy,
    ProjectDirectoryMissing,
    ProjectExists,
    ProjectHeldByExpert,
    ProjectPathInvalid,
    ProjectRequired,
    RootOutsideWhitelist,
    WheelHeldByOther,
)
from app.domain.workbench.models import HandoverRequest
from app.infrastructure.workbench.repository import WorkbenchRepository

WORKSPACE = "/home/u/research"


async def machine(factory, *, owner=OWNER, roots=(WORKSPACE,), sandbox=True):
    async with factory() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            env = await repo.register_environment(
                owner_actor_id=owner,
                name="laptop",
                agent_version="0.1.0",
                codex_version="0.155.1",
                roots=list(roots),
                ceiling={"sandbox": "workspace-write", "network": False},
            )
            sb = None
            if sandbox:
                sb = await repo.register_sandbox(
                    owner_actor_id=owner,
                    app_server_url="ws://sandbox:47800",
                    token_ref="secret/x",
                    codex_version="0.155.1",
                )
        return env, sb


async def project(factory, env, path="hengrui", *, owner=OWNER, title=None):
    async with factory() as s:
        return await ProjectService(WorkbenchRepository(s)).create(
            owner_actor_id=owner,
            environment_id=env,
            workspace_root=WORKSPACE,
            path=path,
            title=title,
        )


async def start(factory, kind, project_id=None, *, owner=OWNER, title=None):
    async with factory() as s:
        return await SessionService(WorkbenchRepository(s)).start(
            owner_actor_id=owner, kind=kind, project_id=project_id, title=title
        )


def ask(session_id, key):
    return HandoverRequest(
        idempotency_key=key,
        session_id=session_id,
        profile_id="DATA_QUERY",
        original_input={"text": "恒瑞医药 2025 年的毛利率"},
        budget_limit=Decimal("2"),
    )


# ---------------------------------------------------------------- 工作区与项目
async def test_workspaces_come_from_the_machines_whitelist(db):
    env, _ = await machine(db, roots=(WORKSPACE, "/home/u/notes"))
    async with db() as s:
        found = await ProjectService(WorkbenchRepository(s)).workspaces(
            owner_actor_id=OWNER
        )
        assert (
            await ProjectService(WorkbenchRepository(s)).workspaces(
                owner_actor_id=OTHER
            )
            == []
        )
    assert [(w["environment_id"], w["root"], w["online"]) for w in found] == [
        (env, WORKSPACE, True),
        (env, "/home/u/notes", True),
    ]
    assert found[0]["environment_name"] == "laptop"


async def test_a_project_is_a_folder_under_a_workspace(db):
    env, _ = await machine(db)
    created = await project(db, env, "2025/恒瑞医药")
    assert created["directory"] == "/home/u/research/2025/恒瑞医药"
    assert created["title"] == "恒瑞医药" and created["archived"] is False
    assert created["environment_name"] == "laptop"
    whole = await project(db, env, "", title=" 整个 工作区 ")
    assert whole["directory"] == WORKSPACE and whole["title"] == "整个 工作区"
    async with db() as s:
        listed = await ProjectService(WorkbenchRepository(s)).listing(
            owner_actor_id=OWNER
        )
    assert [p["path"] for p in listed] == ["", "2025/恒瑞医药"]
    assert listed[1]["conversations"] == 0


async def test_projects_on_a_removed_machine_stay_listed_with_its_name(db):
    """账 179 的另一半：机器移除后项目还在列表里，机器名照显示、算离线；少了这两个字段前端整份清单校验不过。"""
    env, _ = await machine(db)
    created = await project(db, env, "maotai")
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.set_environment_status(env, "offline")
            await repo.remove_environment(env, owner_actor_id=OWNER)
    async with db() as s:
        listed = await ProjectService(WorkbenchRepository(s)).listing(owner_actor_id=OWNER)
    assert [p["title"] for p in listed] == [created["title"]]
    assert listed[0]["environment_name"] == "laptop"
    assert listed[0]["online"] is False


@pytest.mark.parametrize("path", ["../elsewhere", "/etc", "a/../b", "C:\\x"])
async def test_a_project_cannot_leave_its_workspace(db, path):
    """AT-INV-05"""
    env, _ = await machine(db)
    with pytest.raises(ProjectPathInvalid):
        await project(db, env, path)
    async with db() as s:
        assert (
            await s.execute(text("select count(*) from workbench_projects"))
        ).scalar() == 0


class FakeAgent:
    """经会合点问代理目录在不在（0024）。answer 是回包或要抛的异常。"""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def call(self, relay_user, method, params=None):
        self.calls.append((relay_user, method, params))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def paired(factory, owner=OWNER):
    async with factory() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.put_relay_identity(
                owner,
                relay_user="u-1",
                agent_token_ciphertext="a",
                sandbox_token_ciphertext="s",
            )


async def create_with(factory, env, agent, path="财务预测"):
    async with factory() as s:
        return await ProjectService(WorkbenchRepository(s), agent).create(
            owner_actor_id=OWNER,
            environment_id=env,
            workspace_root=WORKSPACE,
            path=path,
        )


async def test_a_project_is_registered_only_when_its_folder_is_on_the_computer(db):
    """2026-10-14 现网：只登记了路径，电脑上没有那个文件夹，工作对话写文件时代理一直报工作目录不存在。"""
    env, _ = await machine(db)
    await paired(db)
    for answer in (AgentRefused(-32012, "not found"), {"type": "file"}):
        agent = FakeAgent(answer)
        with pytest.raises(ProjectDirectoryMissing):
            await create_with(db, env, agent)
        assert agent.calls == [
            ("u-1", "fs/getMetadata", {"path": WORKSPACE + "/财务预测"})
        ]
    async with db() as s:
        assert (
            await s.execute(text("select count(*) from workbench_projects"))
        ).scalar() == 0
    made = await create_with(db, env, FakeAgent({"type": "dir", "size": 0, "mtime": 1}))
    assert made["directory"] == WORKSPACE + "/财务预测"


async def test_a_project_is_still_registered_when_the_computer_cannot_be_asked(db):
    env, _ = await machine(db)
    silent = FakeAgent({"type": "dir"})
    assert (await create_with(db, env, silent, "a"))["path"] == "a"  # 没配对：不问
    assert silent.calls == []
    await paired(db)
    for answer, path in (
        (AgentOffline("agent offline"), "b"),
        (AgentRefused(-32010, "outside"), "c"),
    ):
        assert (await create_with(db, env, FakeAgent(answer), path))["path"] == path
    assert (await create_with(db, env, None, "d"))["path"] == "d"  # 没配通道


async def test_a_workspace_must_be_on_the_whitelist_and_the_machine_mine(db):
    env, _ = await machine(db)
    async with db() as s:
        service = ProjectService(WorkbenchRepository(s))
        with pytest.raises(RootOutsideWhitelist):
            await service.create(
                owner_actor_id=OWNER,
                environment_id=env,
                workspace_root="/home/u",
                path="research/x",
            )
        with pytest.raises(NotFound):
            await service.create(
                owner_actor_id=OTHER,
                environment_id=env,
                workspace_root=WORKSPACE,
                path="x",
            )


async def test_one_live_project_per_directory(db):
    env, _ = await machine(db)
    first = await project(db, env, "hengrui")
    with pytest.raises(ProjectExists):
        await project(db, env, "hengrui")
    with pytest.raises(ProjectExists):
        await project(db, env, "hengrui/")
    async with db() as s:
        service = ProjectService(WorkbenchRepository(s))
        archived = await service.archive(
            first["id"], owner_actor_id=OWNER, archived=True
        )
        assert archived["archived"] is True
        assert await service.listing(owner_actor_id=OWNER) == []
        assert (
            len(await service.listing(owner_actor_id=OWNER, include_archived=True)) == 1
        )
    second = await project(db, env, "hengrui")  # 归档的不占位置
    async with db() as s:
        service = ProjectService(WorkbenchRepository(s))
        with pytest.raises(ProjectExists):  # 恢复时位置已经被占了
            await service.archive(first["id"], owner_actor_id=OWNER, archived=False)
        renamed = await service.rename(
            second["id"], owner_actor_id=OWNER, title="恒瑞医药研究"
        )
        assert renamed["title"] == "恒瑞医药研究"
        with pytest.raises(NotFound):
            await service.rename(second["id"], owner_actor_id=OTHER, title="x")


# ---------------------------------------------------------------- 对话
async def test_chat_needs_no_project_and_work_does(db):
    """AT-INV-04、F-PROJ-02"""
    env, sb = await machine(db)
    chat = await start(db, "chat")
    assert chat["kind"] == "chat" and chat["project_id"] is None
    with pytest.raises(ProjectRequired):
        await start(db, "work")
    made = await project(db, env)
    work = await start(db, "work", made["id"], title="  整理  年报 ")
    async with db() as s:
        repo = WorkbenchRepository(s)
        free = await repo.get_session(chat["session_id"])
        bound = await repo.get_session(work["session_id"])
        events = await repo.list_events(session_id=work["session_id"])
    assert (free["environment_id"], free["project_root"], free["title"]) == (
        None,
        None,
        None,
    )
    assert str(free["sandbox_id"]) == sb
    assert bound["kind"] == "work" and bound["title"] == "整理 年报"
    assert bound["project_root"] == "/home/u/research/hengrui"
    assert str(bound["environment_id"]) == env
    assert events[0]["type"] == "session/created"
    assert events[0]["payload"]["project_id"] == made["id"]


async def test_no_conversation_without_a_sandbox(db):
    await machine(db, sandbox=False)
    with pytest.raises(NoSandbox):
        await start(db, "chat")


async def test_conversations_cannot_be_opened_in_foreign_or_archived_projects(db):
    env, _ = await machine(db)
    await machine(db, owner=OTHER)
    made = await project(db, env)
    with pytest.raises(NotFound):
        await start(db, "chat", made["id"], owner=OTHER)
    with pytest.raises(NotFound):
        await start(db, "chat", str(uuid.uuid4()))
    with pytest.raises(NotFound):
        await start(db, "chat", "not-a-uuid")
    async with db() as s:
        await ProjectService(WorkbenchRepository(s)).archive(
            made["id"], owner_actor_id=OWNER, archived=True
        )
    with pytest.raises(ProjectArchived):
        await start(db, "work", made["id"])


async def test_the_first_message_names_the_conversation(db):
    await machine(db)
    chat = await start(db, "chat")
    first = "恒瑞医药 2023 到 2025 年的盈利能力怎么样，和同行比呢，请给出数据"
    async with db() as s:
        service = SessionService(WorkbenchRepository(s))
        await service.record_user_turn_requested(
            chat["session_id"], owner_actor_id=OWNER, text=first, request_id="r1"
        )
        await service.record_user_turn_requested(
            chat["session_id"], owner_actor_id=OWNER, text="第二句", request_id="r2"
        )
        named = await WorkbenchRepository(s).get_session(chat["session_id"])
        assert named["title"] == first[:30].rstrip() + "…"
        assert len(named["title"]) <= 31
        renamed = await service.rename(
            chat["session_id"], owner_actor_id=OWNER, title="恒瑞的盈利"
        )
        assert renamed["title"] == "恒瑞的盈利"
        with pytest.raises(NotFound):
            await service.rename(chat["session_id"], owner_actor_id=OTHER, title="x")


async def test_a_chat_is_put_into_a_project_once_and_then_becomes_work(db):
    """AT-INV-06 的前一半、AT-INV-07 的账"""
    env, _ = await machine(db)
    made, other = await project(db, env, "a"), await project(db, env, "b")
    chat = await start(db, "chat")
    sid = chat["session_id"]
    async with db() as s:
        service = SessionService(WorkbenchRepository(s))
        with pytest.raises(ProjectRequired):
            await service.to_work(sid, owner_actor_id=OWNER)
        with pytest.raises(ProjectRequired):
            await Ledger(WorkbenchRepository(s)).handover(
                ask(sid, "k-0000-0001"), owner_actor_id=OWNER
            )
        with pytest.raises(NotFound):
            await service.attach_project(
                sid, owner_actor_id=OTHER, project_id=made["id"]
            )
        attached = await service.attach_project(
            sid, owner_actor_id=OWNER, project_id=made["id"]
        )
        assert str(attached["project_id"]) == made["id"]
        assert attached["project_root"] == "/home/u/research/a"
        assert attached["kind"] == "chat"
        again = await service.attach_project(
            sid, owner_actor_id=OWNER, project_id=made["id"]
        )
        assert str(again["project_id"]) == made["id"]
        with pytest.raises(ConversationChangeRefused):
            await service.attach_project(
                sid, owner_actor_id=OWNER, project_id=other["id"]
            )
        worked = await service.to_work(sid, owner_actor_id=OWNER)
        assert worked["kind"] == "work"
        assert (await service.to_work(sid, owner_actor_id=OWNER))["kind"] == "work"
        types = [
            e["type"] for e in await WorkbenchRepository(s).list_events(session_id=sid)
        ]
    assert types == [
        "session/created",
        "session/project_attached",
        "session/kind_changed",
    ]


# ---------------------------------------------------------------- 两条规矩
async def test_one_expert_per_project_at_a_time(db):
    """所有者 2026-09-29 定（I4）"""
    env, _ = await machine(db)
    made, other = await project(db, env, "a"), await project(db, env, "b")
    first = await start(db, "work", made["id"])
    second = await start(db, "chat", made["id"])
    elsewhere = await start(db, "work", other["id"])
    async with db() as s:
        led = Ledger(WorkbenchRepository(s))
        task = await led.handover(
            ask(first["session_id"], "k-0000-0001"), owner_actor_id=OWNER
        )
        # 同一项目的第二位专家排队：建单、不交方向盘、不开车（0017，所有者 2026-10-11「一次一位」）
        queued = await led.handover(
            ask(second["session_id"], "k-0000-0002"), owner_actor_id=OWNER
        )
        assert queued["created"] and queued["queued_behind"] == task["task_id"]
        repo = WorkbenchRepository(s)
        waiting_session = await repo.get_session(second["session_id"])
        assert (
            waiting_session["wheel"] == "user"
            and waiting_session["active_task_id"] is None
        )
        assert waiting_session["kind"] == "expert"  # 交给专家 = 这段对话从此是专家对话
        assert (await repo.active_task_of_project(made["id"]))["id"] == __import__(
            "uuid"
        ).UUID(task["task_id"])
        # 别的项目不受影响
        await led.handover(
            ask(elsewhere["session_id"], "k-0000-0003"), owner_actor_id=OWNER
        )
        stored = await WorkbenchRepository(s).get_task(task["task_id"])
        assert str(stored["project_id"]) == made["id"]
        with pytest.raises(ProjectBusy):
            await ProjectService(WorkbenchRepository(s)).archive(
                made["id"], owner_actor_id=OWNER, archived=True
            )
        # 前一位到终态：排队的那一位自动开始，不用再请
        await led.request_cancel(task["task_id"], owner_actor_id=OWNER)
        started = await repo.get_session(second["session_id"])
        assert started["wheel"] == "advisor"
        assert str(started["active_task_id"]) == queued["task_id"]
        third = await start(db, "chat", made["id"])
        again = await led.handover(
            ask(third["session_id"], "k-0000-0004"), owner_actor_id=OWNER
        )
        assert again["queued_behind"] == queued["task_id"]


async def test_the_database_itself_refuses_a_second_running_delegation(db):
    env, _ = await machine(db)
    made = await project(db, env)
    first = await start(db, "work", made["id"])
    second = await start(db, "work", made["id"])
    async with db() as s:
        await Ledger(WorkbenchRepository(s)).handover(
            ask(first["session_id"], "k-0000-0001"), owner_actor_id=OWNER
        )
    with pytest.raises(IntegrityError, match="uq_workbench_tasks_project_active"):
        async with db() as s, s.begin():
            await s.execute(
                text(
                    """insert into workbench_tasks (id, session_id, owner_actor_id, idempotency_key,
                       request_digest, profile_id, profile_version, original_input, environment_id,
                       project_root, project_id, state, budget)
                       select gen_random_uuid(), :s, owner_actor_id, 'k-direct', request_digest,
                       profile_id, profile_version, original_input, environment_id, project_root,
                       project_id, 'QUEUED', budget from workbench_tasks limit 1"""
                ),
                {"s": second["session_id"]},
            )


async def test_two_requests_for_the_same_project_at_once(db):
    env, _ = await machine(db)
    made = await project(db, env)
    sessions = [await start(db, "work", made["id"]) for _ in range(3)]

    async def one(index):
        async with db() as s:
            made_one = await Ledger(WorkbenchRepository(s)).handover(
                ask(sessions[index]["session_id"], f"k-0000-100{index}"),
                owner_actor_id=OWNER,
            )
            return "queued" if made_one["queued_behind"] else "taken"

    # 同时来三位：项目行锁让它们排成一列，一位在跑、两位排队（0017：一次一位）
    results = await asyncio.gather(*(one(i) for i in range(3)))
    assert sorted(results) == ["queued", "queued", "taken"]


async def test_while_the_expert_works_others_may_chat_but_not_work(db):
    """所有者 2026-09-29 定（I5）"""
    env, _ = await machine(db)
    made = await project(db, env)
    handed = await start(db, "work", made["id"])
    working = await start(db, "work", made["id"])
    chatting = await start(db, "chat", made["id"])
    outside = await start(db, "chat")
    async with db() as s:
        repo = WorkbenchRepository(s)
        service = SessionService(repo)
        task = await Ledger(repo).handover(
            ask(handed["session_id"], "k-0000-0001"), owner_actor_id=OWNER
        )

        async def say(session):
            return await service.record_user_turn_requested(
                session["session_id"],
                owner_actor_id=OWNER,
                text="继续",
                request_id=str(uuid.uuid4()),
            )

        with pytest.raises(WheelHeldByOther):  # AT-INV-13
            await say(handed)
        with pytest.raises(ProjectHeldByExpert) as refused:
            await say(working)
        assert refused.value.details["active_task_id"] == task["task_id"]
        await say(chatting)
        await say(outside)
        with pytest.raises(ProjectHeldByExpert):  # 聊天这时也不能转成工作
            await service.to_work(chatting["session_id"], owner_actor_id=OWNER)
        await Ledger(repo).request_cancel(task["task_id"], owner_actor_id=OWNER)
        await say(working)
        await service.to_work(chatting["session_id"], owner_actor_id=OWNER)


# ---------------------------------------------------------------- 旧入口与旧数据
async def test_the_old_way_of_creating_a_session_still_works_and_gets_a_project(db):
    env, _, sid = await seed(db)
    _, _, second = await seed(db, root="/home/u/research/proj")
    async with db() as s:
        repo = WorkbenchRepository(s)
        first, again = await repo.get_session(sid), await repo.get_session(second)
        made = await repo.get_project(str(first["project_id"]))
    assert first["kind"] == "work" and first["project_root"] == "/home/u/research/proj"
    assert (made["workspace_root"], made["path"], made["title"]) == (
        WORKSPACE,
        "proj",
        "proj",
    )
    assert str(again["environment_id"]) != str(first["environment_id"])
    async with db() as s:
        with pytest.raises(RootOutsideWhitelist):
            await SessionService(WorkbenchRepository(s)).create(
                owner_actor_id=OWNER,
                environment_id=env,
                sandbox_id=str(first["sandbox_id"]),
                project_root="/home/u/elsewhere",
            )


def test_sessions_from_before_the_migration_are_kept_as_work_in_projects():
    """AT-INV-16。迁移到上一版、放进旧样子的数据、再跑这一版。"""
    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set AGENT_TEST_DATABASE_URL to run PostgreSQL workbench tests")
    schema = "wb_mig_" + uuid.uuid4().hex
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    paths = sorted((ROOT / "alembic/versions").glob("20*.py"))
    # 这一版是 0012；后面再加的迁移（0013 知识库…）不参与这个测试
    paths = paths[
        : [p.name for p in paths].index("20260929_0012_workbench_projects.py") + 1
    ]
    assert paths[-1].name == "20260929_0012_workbench_projects.py"

    def run(connection, path):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()

    try:
        with engine.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{schema}"'))
            c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            with Operations.context(MigrationContext.configure(c)):
                for path in paths[:-1]:
                    run(c, path)
                owner = uuid.uuid4()
                envs = {}
                for name, roots in (
                    ("linux", '["/home/u/research", "/home/u"]'),
                    ("windows", '["C:\\\\Users\\\\Me\\\\Research"]'),
                    ("bare", "[]"),
                ):
                    envs[name] = c.execute(
                        text(
                            "insert into workbench_environments (owner_actor_id, name, roots)"
                            " values (:o, :n, cast(:r as jsonb)) returning id"
                        ),
                        {"o": owner, "n": name, "r": roots},
                    ).scalar_one()
                sandbox = c.execute(
                    text(
                        "insert into workbench_sandboxes (owner_actor_id, app_server_url,"
                        " token_ref) values (:o, 'ws://x', 'env:X') returning id"
                    ),
                    {"o": owner},
                ).scalar_one()
                rows = [
                    ("linux", "/home/u/research/a/b"),
                    ("linux", "/home/u/research/a/b"),
                    ("linux", "/home/u/notes"),
                    ("windows", "C:\\Users\\Me\\Research\\茅台"),
                    ("bare", "/srv/anything"),
                ]
                ids = [
                    c.execute(
                        text(
                            "insert into workbench_sessions (owner_actor_id, environment_id,"
                            " sandbox_id, project_root) values (:o, :e, :s, :r) returning id"
                        ),
                        {"o": owner, "e": envs[name], "s": sandbox, "r": root},
                    ).scalar_one()
                    for name, root in rows
                ]
                c.execute(
                    text(
                        "insert into workbench_tasks (session_id, owner_actor_id,"
                        " idempotency_key, request_digest, profile_id, profile_version,"
                        " original_input, environment_id, project_root, state, budget)"
                        " values (:s, :o, 'k', 'd', 'SMOKE', '1', '{}'::jsonb, :e, :r,"
                        " 'SUCCEEDED', '{}'::jsonb)"
                    ),
                    {"s": ids[0], "o": owner, "e": envs["linux"], "r": rows[0][1]},
                )
                run(c, paths[-1])
            sessions = c.execute(
                text(
                    "select s.project_root, s.kind, p.workspace_root, p.path, p.title, p.id"
                    " from workbench_sessions s join workbench_projects p"
                    " on p.id = s.project_id order by s.created_at, s.project_root"
                )
            ).all()
            task_project = c.execute(
                text("select project_id from workbench_tasks")
            ).scalar_one()
        assert len(sessions) == 5 and {row[1] for row in sessions} == {"work"}
        by_root = {row[0]: row[2:5] for row in sessions}
        assert by_root == {
            "/home/u/research/a/b": ("/home/u/research", "a/b", "b"),
            "/home/u/notes": ("/home/u", "notes", "notes"),
            "C:\\Users\\Me\\Research\\茅台": (
                "C:\\Users\\Me\\Research",
                "茅台",
                "茅台",
            ),
            "/srv/anything": ("/srv/anything", "", "anything"),
        }
        assert (
            len({row[5] for row in sessions}) == 4
        )  # 同一个目录的两段会话归同一个项目
        shared = [row[5] for row in sessions if row[0] == "/home/u/research/a/b"]
        assert task_project == shared[0]
    finally:
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


async def test_the_next_expert_starts_when_the_first_one_ends(db):
    """0017 专家团：同一项目一次一位，前一位到终态后一位自动开始。"""
    from app.domain.workbench.states import TaskState

    env, _ = await machine(db)
    made = await project(db, env, "q")
    first = await start(db, "work", made["id"])
    second = await start(db, "chat", made["id"])
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        a = await led.handover(
            ask(first["session_id"], "k-0000-0011"), owner_actor_id=OWNER
        )
        b = await led.handover(
            ask(second["session_id"], "k-0000-0012"), owner_actor_id=OWNER
        )
        assert b["queued_behind"] == a["task_id"]
        await led.transition(a["task_id"], TaskState.CANCELLED, reason={"by": "test"})
        started = await repo.get_task(b["task_id"])
        assert started["original_input"]["client_context"].get("queued_behind") is None
        session = await repo.get_session(second["session_id"])
        assert (
            session["wheel"] == "advisor"
            and str(session["active_task_id"]) == b["task_id"]
        )
        types = [
            e["type"] for e in await repo.list_events(session_id=second["session_id"])
        ]
        assert (
            "task/queued" in types
            and "task/dequeued" in types
            and "wheel/handover" in types
        )
        r = await s.execute(
            text(
                "select payload from workbench_commands where kind = 'task.drive' and session_id = :s"
            ),
            {"s": second["session_id"]},
        )
        assert [row[0]["task_id"] for row in r.all()] == [b["task_id"]]
