"""录磁带：用真的 Codex 跑几段像样的对话，把对面发来的原始消息按轮次存下来。

预览要的样例里，聊天与工作页显示的是 Codex 发来的事件。事件长什么样由 Codex 定，
所以不手写：真跑一遍，录下来，以后离线重放（tests/preview_replay.py）。增量事件不录：账里不记它。
env 与 workbench_modes_driver.py 相同，另加 TAPE_OUT（写到哪个文件）。
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workbench_chain_driver import events_of, migrate, pump  # noqa: E402

from app.application.workbench.ledger import Ledger  # noqa: E402
from app.application.workbench.project_service import ProjectService  # noqa: E402
from app.application.workbench.runner import SandboxLink  # noqa: E402
from app.application.workbench.session_service import SessionService  # noqa: E402
from app.bootstrap.workbench import build_runner  # noqa: E402
from app.infrastructure.storage.postgres import make_session_factory
from app.infrastructure.workbench.publisher import RedisPublisher  # noqa: E402
from app.infrastructure.workbench.repository import WorkbenchRepository  # noqa: E402

OWNER = str(uuid.uuid4())

FILES = {
    "年报要点.md": (
        "# 2025 年报要点（样例，内容是编的）\n\n"
        "- 营业收入同比增长，创新药占比提高\n"
        "- 研发投入继续加大，费用化比例高\n"
        "- 经营现金流为正，应收账款周转放慢\n"
    ),
    "README.md": "这个目录是预览用的样例项目。\n",
}

CONVERSATIONS: dict[str, dict[str, Any]] = {
    "chat": {
        "kind": "chat",
        "in_project": False,
        "prompts": [
            "毛利率和净利率有什么区别？用三句话说清楚。",
            "那自由现金流呢？怎么算？用两句话。",
        ],
    },
    "project_chat": {
        "kind": "chat",
        "in_project": True,
        "prompts": [
            "这个项目里有哪些文件？",
            "读一下 年报要点.md，用两句话概括。",
            "帮我把这两句概括写进 notes/概括.md。",
        ],
    },
    "work": {
        "kind": "work",
        "in_project": True,
        "prompts": [
            "把毛利率、净利率、自由现金流三个指标的含义整理成 notes/指标说明.md，每个指标一句话。",
            "在上一级目录建一个文件 outside.txt，内容写 test。需要批准就申请批准。",
        ],
    },
}

PROJECT = "恒瑞医药"
SAMPLE_WORKSPACE = "/home/demo/research"

tape: list[dict[str, Any]] = []


def anonymous(body: str, *, root: str) -> str:
    """磁带要进仓库：把这台机器的路径、用户名换成样例的。"""
    user = getpass.getuser()
    body = body.replace(root.rstrip("/"), SAMPLE_WORKSPACE)
    body = body.replace(str(Path.home()), "/home/demo")
    return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(user)}(?![A-Za-z0-9_])", "demo", body)


def start_taping() -> None:
    notified = SandboxLink.on_notification
    asked = SandboxLink.on_server_request

    async def on_notification(self, method, params):
        if not method.lower().endswith("delta"):
            tape.append({"kind": "notification", "method": method, "params": params})
        await notified(self, method, params)

    async def on_server_request(self, rid, method, params):
        tape.append({"kind": "request", "method": method, "params": params})
        await asked(self, rid, method, params)

    SandboxLink.on_notification = on_notification  # type: ignore[method-assign]
    SandboxLink.on_server_request = on_server_request  # type: ignore[method-assign]


async def decline_pending(factory, runner, sandbox, sid) -> int:
    """有等着批准的就拒绝，好让这一轮结束。磁带里已经录下了请求。"""
    answered = 0
    async with factory() as s:
        repo = WorkbenchRepository(s)
        events = await repo.list_events(session_id=sid, after_cursor=0, limit=1000)
        for pending in await repo.list_interactions(session_id=sid, status="pending"):
            token = next(
                e["payload"]["token"]
                for e in events
                if e["type"] == "interaction/opened"
                and e["payload"]["interaction_id"] == str(pending["id"])
            )
            await Ledger(repo).respond_interaction(
                str(pending["id"]),
                token=token,
                response={"decision": "decline"},
                owner_actor_id=OWNER,
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sandbox,
                    kind="approval.respond",
                    payload={
                        "request_id": pending["prompt"]["subject"]["request_id"],
                        "decision": "decline",
                    },
                )
            answered += 1
    return answered


def turn_outcome(events: list[dict]) -> str | None:
    if any(e["type"] == "command/failed" for e in events):
        return "failed"
    for event in events:
        if event["type"] == "turn/completed":
            turn = event["payload"].get("turn") or {}
            return (
                "completed"
                if turn.get("status") == "completed" and not turn.get("error")
                else "failed"
            )
    return None


def store_recording(out: Path, body: str, conversations: dict) -> bool:
    if not conversations or any(
        not chat["turns"]
        or any(turn["outcome"] != "completed" for turn in chat["turns"])
        for chat in conversations.values()
    ):
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out.parent, delete=False) as stream:
        tmp = Path(stream.name)
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return True


async def say(factory, runner, sandbox, sid, prompt, *, timeout=300) -> dict[str, Any]:  # noqa: ASYNC109
    seen = await events_of(factory, sid)
    mark = int(seen[-1]["cursor"]) if seen else 0
    start = len(tape)
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
    outcome = None
    declined = 0
    while time.time() < deadline and outcome is None:
        await pump(runner, 1)
        fresh = await events_of(factory, sid, mark)
        outcome = turn_outcome(fresh)
        if outcome is None:
            declined += await decline_pending(factory, runner, sandbox, sid)
    await pump(runner, 1)  # 用量的通知可能晚于这一轮结束
    messages = tape[start:]
    kinds = [
        (m["params"].get("item") or {}).get("type")
        for m in messages
        if m["method"] == "item/completed"
    ]
    asked = [m["method"] for m in messages if m["kind"] == "request"]
    print(
        f"  {outcome or '超时'} {prompt[:24]}… 消息 {len(messages)} "
        f"动作 {kinds} 请求 {asked} 拒绝 {declined}",
        flush=True,
    )
    return {
        "prompt": prompt,
        "ended": outcome is not None,
        "outcome": outcome or "timeout",
        "declined": declined,
        "messages": messages,
    }


async def main() -> int:
    url = os.environ["AGENT_TEST_DATABASE_URL"]
    app_url = os.environ.get("APP_SERVER_URL", "ws://127.0.0.1:47800")
    token = os.environ["APP_SERVER_TOKEN"]
    root = os.environ["ROOT"]
    out = Path(os.environ["TAPE_OUT"])
    env_key = os.environ.get("ENV_KEY", "user-pc")
    async with AsyncExitStack() as cleanup:
        folder = PROJECT
        directory = Path(root) / folder
        try:
            directory.mkdir(parents=True)  # noqa: ASYNC240
        except FileExistsError:
            print(f"--- {directory} 已经在了，不动它。换一个 ROOT，或者先把它挪走")
            return 3
        cleanup.callback(shutil.rmtree, directory)
        schema = "wb_tape_" + uuid.uuid4().hex[:8]
        sync = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
        cleanup.callback(sync.dispose)
        with sync.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{schema}"'))
            c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            migrate(c)

        def drop_schema():
            with sync.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

        cleanup.callback(drop_schema)
        engine = create_async_engine(
            url.replace("postgresql://", "postgresql+asyncpg://"),
            connect_args={"server_settings": {"search_path": schema}},
        )
        cleanup.push_async_callback(engine.dispose)
        factory = make_session_factory(engine)
        runner = build_runner(
            factory,
            publisher=RedisPublisher(None, "it"),
            runner_id="tape-driver",
            environment_key=env_key,
            poll_seconds=0.2,
        )
        for name, body in FILES.items():
            (directory / name).write_text(body)  # noqa: ASYNC240
        cleanup.callback(
            setattr, SandboxLink, "on_notification", SandboxLink.on_notification
        )
        cleanup.callback(
            setattr, SandboxLink, "on_server_request", SandboxLink.on_server_request
        )
        tape.clear()
        start_taping()
        recorded: dict[str, Any] = {}
        try:
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
                    await repo.put_prefs(
                        OWNER, model=None, approval_policy="on-request"
                    )
                project = await ProjectService(repo).create(
                    owner_actor_id=OWNER,
                    environment_id=env,
                    workspace_root=root,
                    path=folder,
                )
            for name, spec in CONVERSATIONS.items():
                print(f"--- {name}", flush=True)
                async with factory() as s:
                    repo = WorkbenchRepository(s)
                    sid = (
                        await SessionService(repo).start(
                            owner_actor_id=OWNER,
                            kind=spec["kind"],
                            project_id=project["id"] if spec["in_project"] else None,
                        )
                    )["session_id"]
                    async with repo.transaction():
                        await repo.enqueue_command(
                            session_id=sid,
                            sandbox_id=sandbox,
                            kind="session.start_thread",
                            payload={},
                        )
                await pump(runner, 2)
                turns = []
                for prompt in spec["prompts"]:
                    turns.append(await say(factory, runner, sandbox, sid, prompt))
                recorded[name] = {
                    "kind": spec["kind"],
                    "in_project": spec["in_project"],
                    "turns": turns,
                }
            made = sorted(
                str(p.relative_to(directory))
                for p in directory.rglob("*")  # noqa: ASYNC240
                if p.is_file()
            )
            print(f"--- 项目目录里现在有：{made}")
            body = anonymous(
                json.dumps(
                    {
                        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "codex": "0.155.1",
                        "model": "kimi-k3",
                        "directory": str(directory),
                        "workspace": root,
                        "files_after": made,
                        "conversations": recorded,
                    },
                    ensure_ascii=False,
                    indent=1,
                    default=str,
                ),
                root=root,
            )
            left = [w for w in (getpass.getuser(), str(Path.home())) if w in body]
            if left:
                print(f"--- 磁带里还有这台机器的东西 {left}，没有写文件")
                return 2
            if "sk-" in body:
                # 不该有。有就不写，先看是什么
                hits = [line for line in body.splitlines() if "sk-" in line]
                print(f"--- 磁带里有疑似密钥的内容，共 {len(hits)} 行，没有写文件")
                return 2
            if not store_recording(out, body, recorded):
                return 1
            print(f"--- 写到 {out}（{len(body)} 字）")
        finally:
            for link in runner.links.values():
                if link.client:
                    await link.client.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
