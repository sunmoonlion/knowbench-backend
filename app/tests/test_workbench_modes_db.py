"""聊天只读、工作可写、专家可写：每一轮发给 Codex 的设置（AT-INV-01 至 03、07）。

对面是够用的假 app-server；真的 Codex 上的验证在联调脚本里。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_runner_db import TOKEN, FakeAppServer, wait_for

from app.application.workbench.ledger import Ledger
from app.application.workbench.project_service import ProjectService
from app.application.workbench.session_service import SessionService
from app.bootstrap.workbench import build_runner
from app.domain.workbench.models import HandoverRequest
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository

WORKSPACE = "/home/u/research"
DIRECTORY = "/home/u/research/hengrui"
READ_ONLY = {"type": "readOnly", "networkAccess": False}
WRITE = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False}
ENVIRONMENT = [{"environmentId": "user-pc", "cwd": DIRECTORY}]
# 子代理与「目标」会在账外起轮次、花钱：每条线起的时候都关掉
NO_SIDE_THREADS = {
    "features.multi_agent": False,
    "features.goals": False,
    # 让模型能在一轮中间问人（approval.md「Codex 反过来问用户」）
    "features.default_mode_request_user_input": True,
    # 模型一次看得到的工具结果放大到 2.5 万记，免得年报、三张表从中间被省掉（待办账 137）
    "tool_output_token_limit": 25_000,
}


async def setup(db, fake, *, approval="untrusted"):
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            env = await repo.register_environment(
                owner_actor_id=OWNER,
                name="pc",
                agent_version="0.1.0",
                codex_version="0.155.1",
                roots=[WORKSPACE],
                ceiling={"sandbox": "workspace-write", "network": False},
            )
            sandbox = await repo.register_sandbox(
                owner_actor_id=OWNER,
                app_server_url=f"ws://127.0.0.1:{fake.port}",
                token_ref=f"inline:{TOKEN}",
                codex_version="0.155.1",
            )
            await repo.put_prefs(OWNER, model="kimi-k3", approval_policy=approval)
        project = await ProjectService(repo).create(
            owner_actor_id=OWNER,
            environment_id=env,
            workspace_root=WORKSPACE,
            path="hengrui",
        )
    return env, sandbox, project["id"]


async def open_conversation(db, sandbox, kind, project_id=None):
    async with db() as s:
        repo = WorkbenchRepository(s)
        sid = (
            await SessionService(repo).start(
                owner_actor_id=OWNER, kind=kind, project_id=project_id
            )
        )["session_id"]
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sandbox,
                kind="session.start_thread",
                payload={},
            )
    return sid


async def say(db, runner, sandbox, sid, text):
    request_id = uuid.uuid4().hex
    async with db() as s:
        repo = WorkbenchRepository(s)
        await SessionService(repo).record_user_turn_requested(
            sid, owner_actor_id=OWNER, text=text, request_id=request_id
        )
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sandbox,
                kind="turn.start",
                payload={"text": text, "request_id": request_id, "by": "user"},
            )
    assert await runner.run_once() == 1

    async def done():
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        accepted = [e for e in events if e["type"] == "turn/accepted"]
        completed = [e for e in events if e["type"] == "turn/completed"]
        return len(completed) >= len(accepted) > 0

    assert await wait_for(done)


def sent(fake, method):
    return [r["params"] for r in fake.requests if r["method"] == method]


def note_of(params):
    (item,) = params["items"]
    assert item["type"] == "message" and item["role"] == "developer"
    (part,) = item["content"]
    assert part["type"] == "input_text"
    return part["text"]


def only_settings(params):
    keys = ("environments", "cwd", "sandboxPolicy", "approvalPolicy")
    return {k: params[k] for k in keys if k in params}


async def closing(runner):
    for link in runner.links.values():
        if link.client:
            await link.client.close()


async def test_a_chat_outside_any_project_is_read_only_and_has_no_machine(db):
    """AT-INV-01、AT-INV-02 的后端一半"""
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sandbox, _ = await setup(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        sid = await open_conversation(db, sandbox, "chat")
        assert await runner.run_once() == 1
        started = sent(fake, "thread/start")[0]
        assert started == {
            "environments": [],
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "config": NO_SIDE_THREADS,
            "model": "kimi-k3",
        }
        await say(db, runner, sandbox, sid, "毛利率是什么")
        turn = sent(fake, "turn/start")[0]
        assert only_settings(turn) == {
            "environments": [],
            "sandboxPolicy": READ_ONLY,
            "approvalPolicy": "never",
        }
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        started_event = next(e for e in events if e["type"] == "session/thread_started")
        assert started_event["payload"]["environment"] is None
        await closing(runner)
    finally:
        await fake.close()


async def test_one_thread_goes_from_chat_to_project_to_work(db):
    """AT-INV-06 的前一半、AT-INV-07：同一条线，每一轮的权限跟着账走。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sandbox, project_id = await setup(db, fake, approval="untrusted")
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        sid = await open_conversation(db, sandbox, "chat")
        assert await runner.run_once() == 1
        await say(db, runner, sandbox, sid, "先聊一句")

        async with db() as s:
            await SessionService(WorkbenchRepository(s)).attach_project(
                sid, owner_actor_id=OWNER, project_id=project_id
            )
        await say(db, runner, sandbox, sid, "看看项目里有什么")

        async with db() as s:
            await SessionService(WorkbenchRepository(s)).to_work(
                sid, owner_actor_id=OWNER
            )
        await say(db, runner, sandbox, sid, "把结论写进 notes.md")

        turns = sent(fake, "turn/start")
        assert [only_settings(t) for t in turns] == [
            {"environments": [], "sandboxPolicy": READ_ONLY, "approvalPolicy": "never"},
            {
                "environments": ENVIRONMENT,
                "cwd": DIRECTORY,
                "sandboxPolicy": READ_ONLY,
                "approvalPolicy": "never",
            },
            {
                "environments": ENVIRONMENT,
                "cwd": DIRECTORY,
                "sandboxPolicy": WRITE,
                "approvalPolicy": "untrusted",
            },
        ]
        assert len({t["threadId"] for t in turns}) == 1  # 同一条线
        assert len(sent(fake, "thread/start")) == 1

        # 每换一种情形，先往线里插一段说明，再发那一轮；说明进账
        order = [
            r["method"]
            for r in fake.requests
            if r["method"] in ("thread/inject_items", "turn/start")
        ]
        assert order == ["thread/inject_items", "turn/start"] * 3
        notes = [note_of(p) for p in sent(fake, "thread/inject_items")]
        assert "no shell" in notes[0]
        assert DIRECTORY in notes[1] and "read-only" in notes[1]
        assert DIRECTORY in notes[2] and "WORK mode" in notes[2]
        assert all("shell tool IS available" in n for n in notes[1:])
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        announced = [
            e["payload"] for e in events if e["type"] == "session/mode_announced"
        ]
        assert [(a["mode"], a["injected"]) for a in announced] == [
            ("chat", True),
            ("chat_in_project", True),
            ("work", True),
        ]
        assert [a["note"] for a in announced] == notes
        await closing(runner)
    finally:
        await fake.close()


async def test_work_starts_writable_with_the_users_approval_policy(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sandbox, project_id = await setup(db, fake, approval="on-failure")
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        sid = await open_conversation(db, sandbox, "work", project_id)
        assert await runner.run_once() == 1
        assert sent(fake, "thread/start")[0] == {
            "environments": ENVIRONMENT,
            "cwd": DIRECTORY,
            "sandbox": "workspace-write",
            # 设置页里的 on-failure，Codex 0.155.1 已经不认：按 on-request 发
            "approvalPolicy": "on-request",
            "config": NO_SIDE_THREADS,
            "model": "kimi-k3",
        }
        await say(db, runner, sandbox, sid, "整理年报要点")
        assert only_settings(sent(fake, "turn/start")[0]) == {
            "environments": ENVIRONMENT,
            "cwd": DIRECTORY,
            "sandboxPolicy": WRITE,
            "approvalPolicy": "on-request",
        }
        await closing(runner)
    finally:
        await fake.close()


async def test_the_expert_works_writable_on_a_conversation_that_is_a_chat(db):
    """专家接着用户那条线做：这段对话是聊天，专家的每一轮仍然是可写的。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sandbox, project_id = await setup(db, fake, approval="on-request")
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        sid = await open_conversation(db, sandbox, "chat", project_id)
        assert await runner.run_once() == 1
        await say(db, runner, sandbox, sid, "恒瑞医药的毛利率")
        async with db() as s:
            task = await Ledger(WorkbenchRepository(s)).handover(
                HandoverRequest(
                    idempotency_key="k-0000-0001",
                    session_id=sid,
                    profile_id="SMOKE",
                    original_input={"text": "项目里有什么"},
                    budget_limit=Decimal("2"),
                ),
                owner_actor_id=OWNER,
            )
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sandbox,
                    kind="task.drive",
                    payload={"task_id": task["task_id"]},
                )
        assert await runner.run_once() == 1
        await runner.wait_drivers(timeout=30)
        turns = sent(fake, "turn/start")
        assert only_settings(turns[0])["sandboxPolicy"] == READ_ONLY
        assert len(turns) >= 2
        for turn in turns[1:]:
            assert only_settings(turn) == {
                "environments": ENVIRONMENT,
                "cwd": DIRECTORY,
                "sandboxPolicy": WRITE,
                "approvalPolicy": "on-request",
            }
        assert len({t["threadId"] for t in turns}) == 1
        async with db() as s:
            session = await WorkbenchRepository(s).get_session(sid)
        assert session["kind"] == "expert"  # 交给专家 = 这段对话从此是专家对话（0017）
        await closing(runner)
    finally:
        await fake.close()


async def test_sessions_made_the_old_way_keep_working(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        env, sandbox, _ = await setup(db, fake, approval="never")
        async with db() as s:
            repo = WorkbenchRepository(s)
            sid = (
                await SessionService(repo).create(
                    owner_actor_id=OWNER,
                    environment_id=env,
                    sandbox_id=sandbox,
                    project_root="/home/u/research/old",
                    thread_settings={
                        "approvalPolicy": "untrusted",
                        "sandbox": "workspace-write",
                    },
                )
            )["session_id"]
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sandbox,
                    kind="session.start_thread",
                    payload={},
                )
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        assert await runner.run_once() == 1
        assert sent(fake, "thread/start")[0] == {
            "cwd": "/home/u/research/old",
            "environments": [
                {"environmentId": "user-pc", "cwd": "/home/u/research/old"}
            ],
            "approvalPolicy": "untrusted",
            "sandbox": "workspace-write",
            "config": NO_SIDE_THREADS,
        }
        await say(db, runner, sandbox, sid, "hello")
        turn = only_settings(sent(fake, "turn/start")[0])
        assert turn["sandboxPolicy"] == WRITE and turn["approvalPolicy"] == "untrusted"
        assert turn["cwd"] == "/home/u/research/old"
        await closing(runner)
    finally:
        await fake.close()


async def test_the_same_mode_is_announced_once(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sandbox, project_id = await setup(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        sid = await open_conversation(db, sandbox, "work", project_id)
        assert await runner.run_once() == 1
        for text in ("第一句", "第二句", "第三句"):
            await say(db, runner, sandbox, sid, text)
        assert len(sent(fake, "turn/start")) == 3
        assert len(sent(fake, "thread/inject_items")) == 1
        await closing(runner)
    finally:
        await fake.close()


async def test_a_turn_still_goes_out_when_the_note_cannot_be_injected(db):
    """对面不认插说明的请求：这一轮照发，权限照样随这一轮带上；账里记着没插成。"""
    fake = FakeAppServer()
    fake.refuse_inject = True
    await fake.start()
    try:
        _, sandbox, project_id = await setup(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        sid = await open_conversation(db, sandbox, "chat", project_id)
        assert await runner.run_once() == 1
        await say(db, runner, sandbox, sid, "看看项目里有什么")
        await say(db, runner, sandbox, sid, "再看一眼")
        turns = sent(fake, "turn/start")
        assert len(turns) == 2
        assert all(only_settings(t)["sandboxPolicy"] == READ_ONLY for t in turns)
        assert len(sent(fake, "thread/inject_items")) == 1  # 不成也不反复试
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        announced = [
            e["payload"] for e in events if e["type"] == "session/mode_announced"
        ]
        assert [(a["mode"], a["injected"]) for a in announced] == [
            ("chat_in_project", False)
        ]
        await closing(runner)
    finally:
        await fake.close()


async def test_a_resumed_thread_keeps_side_threads_off(db):
    """沙箱回收再拉起：重新装载这条线时也带上「不起子代理」的设置。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sandbox, project_id = await setup(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        sid = await open_conversation(db, sandbox, "work", project_id)
        assert await runner.run_once() == 1
        client = runner.links[sandbox].client
        assert client is not None
        await client.close()
        fake.restart()
        await say(db, runner, sandbox, sid, "接着做")
        (resumed,) = sent(fake, "thread/resume")
        assert resumed["config"] == NO_SIDE_THREADS
        await closing(runner)
    finally:
        await fake.close()
