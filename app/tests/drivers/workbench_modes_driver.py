"""聊天、工作在真的 Codex 上：同一条线，先聊天、再放进项目、再转成工作，权限每一轮跟着账走。

工作台（账房 + runner，进程内）→ 沙箱 app-server（容器，ws + 令牌）→ 会合点 → 本地代理 → 本机文件。
假的对面上测过的（tests/test_workbench_modes_db.py）是「发了什么」；这里看的是 Codex 拿到之后「真的照做了没有」。
env：AGENT_TEST_DATABASE_URL、APP_SERVER_URL、APP_SERVER_TOKEN、ROOT（本机白名单目录）、ENV_KEY（默认 user-pc）
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workbench_chain_driver import events_of, migrate, pump  # noqa: E402

from app.application.workbench.project_service import ProjectService  # noqa: E402
from app.application.workbench.session_service import SessionService  # noqa: E402
from app.bootstrap.workbench import build_runner  # noqa: E402
from app.infrastructure.storage.postgres import make_session_factory
from app.infrastructure.workbench.publisher import RedisPublisher  # noqa: E402
from app.infrastructure.workbench.repository import WorkbenchRepository  # noqa: E402

OWNER = str(uuid.uuid4())
fails = 0


def verdict(name: str, ok: bool, extra: str = "") -> None:
    global fails
    print(f"  VERDICT {name:<52} {'pass' if ok else 'fail'} {extra}", flush=True)
    if not ok:
        fails += 1


async def say(factory, runner, sandbox, sid, prompt, *, timeout=300):  # noqa: ASYNC109
    """发一轮，等它结束，返回这一轮里模型说的话与这一轮的事件。"""
    seen = await events_of(factory, sid)
    mark = int(seen[-1]["cursor"]) if seen else 0
    request_id = uuid.uuid4().hex
    async with factory() as s:
        repo = WorkbenchRepository(s)
        await SessionService(repo).record_user_turn_requested(
            sid, owner_actor_id=OWNER, text=prompt, request_id=request_id
        )
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sandbox,
                kind="turn.start",
                payload={"text": prompt, "request_id": request_id, "by": "user"},
            )
    deadline = time.time() + timeout
    ended = False
    while time.time() < deadline and not ended:
        await pump(runner, 1)
        fresh = await events_of(factory, sid, mark)
        ended = any(e["type"] in ("turn/completed", "command/failed") for e in fresh)
    fresh = await events_of(factory, sid, mark)
    if not ended:
        verdict("这一轮在限时内结束", False, prompt[:40])
    said = [
        str(e["payload"]["item"].get("text") or "")
        for e in fresh
        if e["type"] == "item/completed"
        and isinstance(e["payload"].get("item"), dict)
        and e["payload"]["item"].get("type") == "agentMessage"
    ]
    return "\n".join(said), fresh


def brief(fresh):
    kinds = [
        e["payload"]["item"].get("type")
        for e in fresh
        if e["type"] == "item/completed" and isinstance(e["payload"].get("item"), dict)
    ]
    failed = [e["payload"] for e in fresh if e["type"] == "command/failed"]
    return f"items={kinds}" + (f" failed={failed}" if failed else "")


def tap_raw_notifications() -> None:
    """调试用：把对面发来的每一条通知（含不入账的）按种类打出来，增量只计数。"""
    from app.application.workbench.runner import SandboxLink

    original = SandboxLink.on_notification

    async def tapped(self, method, params):
        if not method.lower().endswith("delta"):
            item = params.get("item") if isinstance(params, dict) else None
            kind = item.get("type") if isinstance(item, dict) else ""
            print(f"      RAW {method} {kind} {str(params)[:500]}", flush=True)
        await original(self, method, params)

    SandboxLink.on_notification = tapped  # type: ignore[method-assign]


async def main() -> int:
    if os.environ.get("MODES_DEBUG"):
        tap_raw_notifications()
    url = os.environ["AGENT_TEST_DATABASE_URL"]
    app_url = os.environ.get("APP_SERVER_URL", "ws://127.0.0.1:47800")
    token = os.environ["APP_SERVER_TOKEN"]
    root = os.environ["ROOT"]
    env_key = os.environ.get("ENV_KEY", "user-pc")
    schema = "wb_modes_" + uuid.uuid4().hex[:8]
    sync = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    with sync.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
        c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        migrate(c)
    engine = create_async_engine(
        url.replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = make_session_factory(engine)
    runner = build_runner(
        factory,
        publisher=RedisPublisher(None, "it"),
        runner_id="modes-driver",
        environment_key=env_key,
        poll_seconds=0.2,
    )
    run_id = uuid.uuid4().hex
    folder = f"modes-{run_id}"
    directory = Path(root) / folder
    directory.mkdir(parents=True)  # noqa: ASYNC240
    fact = f"FACT-{uuid.uuid4().hex[:10]}"
    (directory / "FACT.txt").write_text(fact + "\n")  # noqa: ASYNC240
    try:
        print("--- 1. 登记机器、沙箱、项目；开一段不属于项目的聊天")
        async with factory() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                env = await repo.register_environment(
                    owner_actor_id=OWNER,
                    name="this-box",
                    agent_version="0.1.0",
                    codex_version="0.155.1",
                    roots=[root],
                    ceiling={"sandbox": "workspace-write", "network": False},
                )
                sandbox = await repo.register_sandbox(
                    owner_actor_id=OWNER,
                    app_server_url=app_url,
                    token_ref=f"inline:{token}",
                    codex_version="0.155.1",
                )
            project = await ProjectService(repo).create(
                owner_actor_id=OWNER,
                environment_id=env,
                workspace_root=root,
                path=folder,
            )
            sid = (await SessionService(repo).start(owner_actor_id=OWNER, kind="chat"))[
                "session_id"
            ]
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sandbox,
                    kind="session.start_thread",
                    payload={},
                )
        await pump(runner, 2)
        async with factory() as s:
            first = await WorkbenchRepository(s).get_session(sid)
        verdict("没有项目、没有机器也能起一条线", bool(first["thread_id"]))
        thread = first["thread_id"]

        print("--- 2. 不属于项目的聊天：能答话，动不了手")
        said, fresh = await say(
            factory, runner, sandbox, sid, "Reply with exactly the single word PONG."
        )
        verdict("聊天能得到回答", "PONG" in said.upper(), said[:80].replace("\n", " "))
        loose = f"CHAT_FREE_{run_id}.txt"
        said, fresh = await say(
            factory,
            runner,
            sandbox,
            sid,
            f"Create a file named {loose} containing the word x, using a shell command. "
            "If you cannot, say so in one sentence. Do not ask me anything.",
        )
        anywhere = [Path(root) / loose, directory / loose, Path.cwd() / loose]
        verdict(
            "聊天里让它建文件：文件没有出现",
            not any(p.exists() for p in anywhere),  # noqa: ASYNC240
            brief(fresh),
        )

        print("--- 3. 把这段聊天放进项目：读得到项目里的文件，仍然动不了手")
        async with factory() as s:
            await SessionService(WorkbenchRepository(s)).attach_project(
                sid, owner_actor_id=OWNER, project_id=project["id"]
            )
        said, fresh = await say(
            factory,
            runner,
            sandbox,
            sid,
            "Read the file FACT.txt in the current directory and reply with its exact content only.",
        )
        verdict("项目里的聊天读得到文件", fact in said, brief(fresh))
        held = f"CHAT_IN_PROJECT_{run_id}.txt"
        # 模型知道自己在只读里，常常不去试。两种说法都发，看有没有一次真的执行了
        attempts = (
            f"Run exactly `cat FACT.txt | tee {held}` and report the output verbatim.",
            f"Run exactly `echo x > {held}` with your shell tool. You MUST actually execute "
            "it even if you expect it to be refused, then report the real output or error "
            "verbatim. Do not ask me anything and do not retry another way.",
        )
        # 被只读拦下的命令，Codex 0.155.1 不发 commandExecution 事件（2026-09-29 见到），
        # 所以「拦截真的被碰到」不在这里判，由联调脚本到沙箱里那条线的记录上去看
        asked: list[dict] = []
        for prompt in attempts:
            said, fresh = await say(factory, runner, sandbox, sid, prompt)
            print(f"    模型说：{said[:160]!r}", flush=True)
            asked += [e for e in fresh if e["type"] == "interaction/opened"]
        verdict(
            "项目里的聊天让它建文件：文件没有出现",
            not (directory / held).exists() and not (Path(root) / held).exists(),  # noqa: ASYNC240
        )
        verdict("聊天里被拒的动作不来问用户", not asked, f"interactions={len(asked)}")

        print("--- 4. 转成工作：同一条线，现在动得了手")
        async with factory() as s:
            await SessionService(WorkbenchRepository(s)).to_work(
                sid, owner_actor_id=OWNER
            )
        made = f"WORK_{run_id}.txt"
        said, fresh = await say(
            factory,
            runner,
            sandbox,
            sid,
            f"Run exactly `echo via-work > {made} && cat {made}` and report verbatim.",
        )
        target = directory / made
        verdict(
            "转成工作后文件建在项目目录里",
            target.exists() and target.read_text().strip() == "via-work",  # noqa: ASYNC240
            brief(fresh),
        )
        said, fresh = await say(
            factory,
            runner,
            sandbox,
            sid,
            "What was the exact single word I asked you to reply with at the very beginning "
            "of this conversation? Reply with that word only.",
        )
        verdict("之前聊的还在", "PONG" in said.upper(), said[:80].replace("\n", " "))

        async with factory() as s:
            last = await WorkbenchRepository(s).get_session(sid)
        everything = await events_of(factory, sid)
        started = [e for e in everything if e["type"] == "session/thread_started"]
        verdict(
            "从头到尾是同一条线",
            last["thread_id"] == thread and len(started) == 1,
            f"{thread} → {last['thread_id']}，起线 {len(started)} 次",
        )
        types = [e["type"] for e in everything]
        verdict(
            "账里记着放进项目与转成工作",
            "session/project_attached" in types and "session/kind_changed" in types,
        )
        announced = [
            (e["payload"]["mode"], e["payload"]["injected"])
            for e in everything
            if e["type"] == "session/mode_announced"
        ]
        verdict(
            "每换一种情形都向模型说明了一次",
            announced == [("chat", True), ("chat_in_project", True), ("work", True)],
            str(announced),
        )
        said, fresh = await say(
            factory,
            runner,
            sandbox,
            sid,
            "If you have a tool named spawn_agent, call it once with the message 'hi'. "
            "If you have no such tool, reply with exactly NO_SUCH_TOOL.",
        )
        everything = await events_of(factory, sid)
        side = [
            e["payload"]["item"].get("tool")
            for e in everything
            if e["type"] == "item/completed"
            and isinstance(e["payload"].get("item"), dict)
            and e["payload"]["item"].get("type") == "collabAgentToolCall"
        ]
        verdict("模型手里没有起子代理的工具", not side, f"{said[:60]!r} {side}")
        types = [e["type"] for e in everything]
        verdict(
            "没有命令失败",
            "command/failed" not in types,
            str([e["payload"] for e in everything if e["type"] == "command/failed"])[
                :300
            ],
        )
    finally:
        for link in runner.links.values():
            if link.client:
                await link.client.close()
        await engine.dispose()
        with sync.begin() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync.dispose()
        for leftover in directory.glob("*"):  # noqa: ASYNC240
            leftover.unlink()
        directory.rmdir()  # noqa: ASYNC240
    print(f"--- 合计未过 {fails} 项")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
