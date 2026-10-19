"""runner 与 app-server 客户端：假 app-server（进程内 WebSocket）+ 真数据库。
验：thread/start 落 thread_id、turn 命令、通知投影成事件（delta 不落）、用户自驾时审批变 Interaction 并把决定回给 app-server、命令认领互斥。"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import OWNER  # noqa: F401  (fixture)
from test_workbench_ledger_db import db as db
from usage_support import MODEL, UsageMeter
from websockets.asyncio.server import serve

from app.application.workbench.ledger import Ledger
from app.application.workbench.runner import event_kind, thread_id_of
from app.application.workbench.session_service import SessionService
from app.bootstrap.workbench import build_runner
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository

TOKEN = "cap-token-for-tests"


class FakeAppServer:
    """够用的 app-server：initialize、thread/start、thread/resume、turn/start、turn/interrupt；turn 里按输入文本决定要不要发审批请求。

    线程分两处记：`disk`（rollout 落了盘）与 `live`（当前进程装载着）。`restart()` 模拟沙箱回收再拉起（进程换了、盘还在），
    `wipe()` 模拟连盘都没了。`strict_threads` 打开后，对没装载的线程发 turn 会像真 app-server 一样回 thread not found。"""

    def __init__(self):
        self.server = None
        self.disk: set[str] = set()
        self.live: set[str] = set()
        self.strict_threads = False
        self.meter = UsageMeter()
        self.refuse_inject = False  # 打开后像不认这个请求的旧 app-server
        self.port = 0
        self.elicitations: list[dict] = []
        self.requests: list[dict] = []
        self.auth_headers: list[str | None] = []
        self.approval_decisions: list[dict] = []
        self.question_answers: list[dict] = []
        # 线程上还在跑的那一轮（上一任 runner 开的）：thread/resume 时照真 app-server 报 inProgress
        self.in_progress: dict[str, str] = {}
        self.stubborn = False  # 打开后中断不管用：那一轮一直在跑
        self.ready = asyncio.Event()

    async def start(self):
        async def handler(ws):
            self.auth_headers.append(ws.request.headers.get("Authorization"))
            async for raw in ws:
                msg = json.loads(raw)
                if "method" in msg and "id" in msg:
                    self.requests.append(msg)
                    asyncio.create_task(self.handle(ws, msg))
                elif "id" in msg:  # response to our server request
                    self.approval_decisions.append(msg)
                    fut = self._pending.pop(msg["id"], None)
                    if fut:
                        fut.set_result(msg.get("result"))

        self._pending: dict[str, asyncio.Future] = {}
        self.server = await serve(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, ws, msg):
        m, p, rid = msg["method"], msg.get("params") or {}, msg["id"]
        send = lambda o: ws.send(json.dumps(o))  # noqa: E731
        if m == "initialize":
            await send(
                {
                    "id": rid,
                    "result": {"userAgent": "fake/0.155.1", "codexHome": "/data/codex"},
                }
            )
        elif m == "thread/start":
            tid = f"thread-{uuid.uuid4().hex[:8]}"
            self.disk.add(tid)
            self.live.add(tid)
            await send(
                {
                    "id": rid,
                    "result": {
                        "thread": {"id": tid, "cwd": p.get("cwd")},
                        "model": MODEL,
                    },
                }
            )
            await send({"method": "thread/started", "params": {"thread": {"id": tid}}})
        elif m == "thread/resume":
            tid = p["threadId"]
            if tid in self.disk:
                self.live.add(tid)
                turns = (
                    [{"id": self.in_progress[tid], "status": "inProgress"}]
                    if tid in self.in_progress
                    else []
                )
                await send(
                    {"id": rid, "result": {"thread": {"id": tid, "turns": turns}}}
                )
            else:
                await send(
                    {
                        "id": rid,
                        "error": {
                            "code": -32600,
                            # 真 app-server 的文案（thread-store/local/read_thread.rs），和 turn/start 的不一样
                            "message": f"no rollout found for thread id {tid}",
                        },
                    }
                )
        elif (
            m == "turn/start" and self.strict_threads and p["threadId"] not in self.live
        ):
            await send(
                {
                    "id": rid,
                    "error": {
                        "code": -32600,
                        "message": f"thread not found: {p['threadId']}",
                    },
                }
            )
        elif m == "turn/start":
            tid = p["threadId"]
            turn_id = f"turn-{uuid.uuid4().hex[:6]}"
            await send(
                {"id": rid, "result": {"turn": {"id": turn_id, "threadId": tid}}}
            )
            await send(
                {
                    "method": "turn/started",
                    "params": {"threadId": tid, "turn": {"id": turn_id}},
                }
            )
            await send(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": tid, "turnId": turn_id, "delta": "he"},
                }
            )
            text = p["input"][0]["text"]
            # "QUERY 数据集名 数据集名"：每个名字查一次，知识服务都答没有这个数据集
            if text.startswith("QUERY "):
                for n, name in enumerate(text.split()[1:]):
                    await send(
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": tid,
                                "turnId": turn_id,
                                "item": {
                                    "type": "mcpToolCall",
                                    "id": f"mcp-q{n}",
                                    "server": "sunmoon_knowbase",
                                    "tool": "describe_schema",
                                    "status": "failed",
                                    "arguments": {"dataset": name},
                                    "result": {
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": f"unknown dataset: {name}; "
                                                "call list_datasets to see what exists",
                                            }
                                        ]
                                    },
                                    "error": None,
                                },
                            },
                        }
                    )
            if "NEED_ELICITATION" in text:
                srid = f"elic-{uuid.uuid4().hex[:6]}"
                fut = asyncio.get_running_loop().create_future()
                self._pending[srid] = fut
                await send(
                    {
                        "id": srid,
                        "method": "mcpServer/elicitation/request",
                        "params": {
                            "message": "sunmoon_knowbase wants to run run_sql",
                            "mode": "form",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {
                                    "confirm": {"type": "boolean"},
                                    "note": {"type": "string", "default": "ok"},
                                },
                            },
                        },
                    }
                )
                answer = await asyncio.wait_for(fut, 20)
                self.elicitations.append(answer)
                await send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {
                                "type": "mcpToolCall",
                                "id": "mcp-1",
                                "server": "sunmoon_knowbase",
                                "tool": "run_sql",
                                "status": "completed"
                                if answer.get("action") == "accept"
                                else "failed",
                            },
                        },
                    }
                )
            # 审批与提问的触发词：工作线的文字里、或专家委托的问题里带上就触发
            asks: list[tuple[str, dict]] = []
            command_ask = {
                "command": "touch /home/u/x",
                "cwd": "/home/u/research/proj",
                "reason": "write outside workspace",
            }
            file_ask = {"reason": "apply patch inside the project"}
            if "NEED_APPROVAL_TWICE" in text:
                asks = [("command", command_ask), ("command", command_ask)]
            elif "NEED_APPROVAL_THEN_FILE" in text:
                asks = [("command", command_ask), ("file", file_ask)]
            elif "NEED_FILE_APPROVAL" in text:
                asks = [("file", file_ask)]
            elif "NEED_APPROVAL" in text:
                asks = [("command", command_ask)]
            cancelled = False
            for n, (what, extra) in enumerate(asks, start=1):
                srid = f"req-{uuid.uuid4().hex[:6]}"
                fut = asyncio.get_running_loop().create_future()
                self._pending[srid] = fut
                method = (
                    "item/commandExecution/requestApproval"
                    if what == "command"
                    else "item/fileChange/requestApproval"
                )
                await send(
                    {
                        "id": srid,
                        "method": method,
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "itemId": f"it-{n}",
                            "environmentId": "user-pc",
                            **extra,
                        },
                    }
                )
                decision = await asyncio.wait_for(fut, 20)
                if decision.get("decision") == "cancel":
                    cancelled = True
                    break
                await send(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "item": {
                                "type": "commandExecution"
                                if what == "command"
                                else "fileChange",
                                "id": f"it-{n}",
                                "status": "completed"
                                if decision.get("decision") != "decline"
                                else "declined",
                            },
                        },
                    }
                )
            if "NEED_QUESTION" in text and not cancelled:
                srid = f"ask-{uuid.uuid4().hex[:6]}"
                fut = asyncio.get_running_loop().create_future()
                self._pending[srid] = fut
                await send(
                    {
                        "id": srid,
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": tid,
                            "turnId": turn_id,
                            "itemId": "ask-1",
                            "questions": [
                                {
                                    "id": "q1",
                                    "header": "年份",
                                    "question": "看哪一年的年报？",
                                    "isOther": True,
                                    "isSecret": False,
                                    "options": [
                                        {"label": "2024", "description": "最新"},
                                        {"label": "2023", "description": ""},
                                    ],
                                }
                            ],
                            "isBlocking": True,
                        },
                    }
                )
                answer = await asyncio.wait_for(fut, 20)
                self.question_answers.append(answer)
            if cancelled:
                await send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": tid,
                            "turn": {"id": turn_id, "status": "interrupted"},
                        },
                    }
                )
                return
            await send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": tid,
                        "turnId": turn_id,
                        "item": {
                            "type": "agentMessage",
                            "id": "msg-1",
                            "text": f"echo: {text}",
                        },
                    },
                }
            )
            await send(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": tid,
                        "turnId": turn_id,
                        "tokenUsage": self.meter.report(tid, 1000),
                    },
                }
            )
            await send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": tid,
                        "turn": {"id": turn_id, "status": "completed"},
                    },
                }
            )
        elif m == "turn/interrupt":
            if not self.stubborn and self.in_progress.get(p.get("threadId")) == p.get(
                "turnId"
            ):
                self.in_progress.pop(p["threadId"], None)
            await send({"id": rid, "result": {}})
        elif m == "thread/read":
            tid = p["threadId"]
            turns = (
                [{"id": self.in_progress[tid], "status": "inProgress"}]
                if tid in self.in_progress
                else []
            )
            await send({"id": rid, "result": {"thread": {"id": tid, "turns": turns}}})
        elif m == "thread/inject_items" and not self.refuse_inject:
            await send({"id": rid, "result": {}})
        else:
            await send(
                {"id": rid, "error": {"code": -32601, "message": f"unknown {m}"}}
            )

    def restart(self):
        """沙箱回收再拉起：新进程，盘上的 rollout 还在。"""
        self.live.clear()
        self.strict_threads = True

    def wipe(self):
        """连持久卷都没了。"""
        self.live.clear()
        self.disk.clear()
        self.strict_threads = True

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


async def turn_completed(db, sid: str, n: int = 1) -> bool:
    """等会话里出现第 n 条 turn/completed。关连接前要等：不然通知还在写库，下一个用例会被悬着的事务锁住。"""

    async def done():
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        return sum(1 for e in events if e["type"] == "turn/completed") >= n

    return await wait_for(done)


async def wait_for(pred, timeout=8.0, step=0.05):  # noqa: ASYNC109
    for _ in range(int(timeout / step)):
        if await pred():
            return True
        await asyncio.sleep(step)
    return False


async def seed(db, fake):
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            env = await repo.register_environment(
                owner_actor_id=OWNER,
                name="pc",
                agent_version="0.1.0",
                codex_version="0.155.1",
                roots=["/home/u/research"],
                ceiling={"sandbox": "workspace-write", "network": False},
            )
            sb = await repo.register_sandbox(
                owner_actor_id=OWNER,
                app_server_url=f"ws://127.0.0.1:{fake.port}",
                token_ref=f"inline:{TOKEN}",
                codex_version="0.155.1",
            )
        sid = (
            await SessionService(repo).create(
                owner_actor_id=OWNER,
                environment_id=env,
                sandbox_id=sb,
                project_root="/home/u/research/proj",
            )
        )["session_id"]
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid, sandbox_id=sb, kind="session.start_thread", payload={}
            )
        return env, sb, sid


def test_helpers():
    assert (
        event_kind("item/completed") == "item"
        and event_kind("thread/tokenUsage") == "usage"
        and event_kind("thread/environment/disconnected") == "environment"
        and event_kind("turn/completed") == "turn"
    )
    assert (
        thread_id_of({"threadId": "a"}) == "a"
        and thread_id_of({"thread": {"id": "b"}}) == "b"
        and thread_id_of({"turn": {"threadId": "c"}}) == "c"
        and thread_id_of({}) is None
    )


async def test_start_thread_and_user_turn_project_events(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        assert await runner.run_once() == 1  # session.start_thread
        async with db() as s:
            sess = await WorkbenchRepository(s).get_session(sid)
        assert sess["thread_id"] and sess["thread_id"].startswith("thread-")
        assert fake.auth_headers[0] == f"Bearer {TOKEN}"
        assert [r["method"] for r in fake.requests][:2] == [
            "initialize",
            "thread/start",
        ]
        # 用户发 turn
        async with db() as s:
            repo = WorkbenchRepository(s)
            await SessionService(repo).record_user_turn_requested(
                sid, owner_actor_id=OWNER, text="hello", request_id="req-1"
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={"text": "hello", "request_id": "req-1", "by": "user"},
                )
        assert await runner.run_once() == 1

        async def turn_done():
            async with db() as s:
                return any(
                    e["type"] == "turn/completed"
                    for e in await WorkbenchRepository(s).list_events(session_id=sid)
                )

        assert await wait_for(turn_done)
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        types = [e["type"] for e in events]
        assert (
            "session/thread_started" in types
            and "turn/requested" in types
            and "turn/accepted" in types
        )
        assert (
            "item/completed" in types
            and "thread/tokenUsage/updated" in types
            and "turn/completed" in types
        )
        assert not any(t.endswith("/delta") for t in types)  # 流式 delta 不落库
        assert [e["cursor"] for e in events] == list(
            range(1, len(events) + 1)
        )  # 游标连续
        msg = next(e for e in events if e["type"] == "item/completed")
        assert msg["payload"]["item"]["text"] == "echo: hello" and msg["kind"] == "item"
        for link in runner.links.values():
            await link.client.close()
    finally:
        await fake.close()


async def test_user_wheel_approval_becomes_interaction_and_decision_returns(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": "please NEED_APPROVAL now",
                        "request_id": "req-2",
                        "by": "user",
                    },
                )
        await runner.run_once()

        async def opened():
            async with db() as s:
                return (
                    len(
                        await WorkbenchRepository(s).list_interactions(
                            session_id=sid, status="pending"
                        )
                    )
                    == 1
                )

        assert await wait_for(opened)
        async with db() as s:
            repo = WorkbenchRepository(s)
            it = (await repo.list_interactions(session_id=sid, status="pending"))[0]
            assert it["kind"] == "tool_approval" and it["task_id"] is None
            token = next(
                e["payload"]["token"]
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "interaction/opened"
            )
            # 网页上的回答：消费 Interaction，再排一条 approval.respond 命令（与路由层做的一样）
            await Ledger(repo).respond_interaction(
                str(it["id"]),
                token=token,
                response={"decision": "accept"},
                owner_actor_id=OWNER,
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="approval.respond",
                    payload={
                        "request_id": it["prompt"]["subject"]["request_id"],
                        "decision": "accept",
                    },
                )
        assert await runner.run_once() == 1

        async def decided():
            return len(fake.approval_decisions) == 1

        assert await wait_for(decided)
        assert fake.approval_decisions[0]["result"] == {"decision": "accept"}

        async def done():
            async with db() as s:
                return any(
                    e["type"] == "turn/completed"
                    for e in await WorkbenchRepository(s).list_events(session_id=sid)
                )

        assert await wait_for(done)
        async with db() as s:
            r = await s.execute(
                __import__("sqlalchemy").text(
                    "select decision, source from workbench_approval_log where session_id = :s"
                ),
                {"s": sid},
            )
            assert [tuple(x) for x in r.all()] == [("accept", "user")]
        for link in runner.links.values():
            await link.client.close()
    finally:
        await fake.close()


async def test_turn_refused_when_advisor_holds_wheel(db):
    from decimal import Decimal

    from app.domain.workbench.models import HandoverRequest

    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        async with db() as s:
            repo = WorkbenchRepository(s)
            await Ledger(repo).handover(
                HandoverRequest(
                    idempotency_key="k-0000-0009",
                    session_id=sid,
                    profile_id="DATA_QUERY",
                    original_input={"text": "x"},
                    budget_limit=Decimal("5"),
                ),
                owner_actor_id=OWNER,
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={"text": "sneaky", "request_id": "req-3", "by": "user"},
                )
        await runner.run_once()
        async with db() as s:
            repo = WorkbenchRepository(s)
            events = await repo.list_events(session_id=sid)
            failed = [e for e in events if e["type"] == "command/failed"]
            assert (
                failed and "wheel is held by advisor" in failed[0]["payload"]["error"]
            )
            r = await s.execute(
                __import__("sqlalchemy").text(
                    # 交接后没人推的委托，对账会补一条 task.drive（0034），这里只看起线和那一轮
                    "select status from workbench_commands where session_id = :s and kind <> 'task.drive' order by created_at"
                ),
                {"s": sid},
            )
            assert [x[0] for x in r.all()] == ["done", "failed"]
        assert not any(r["method"] == "turn/start" for r in fake.requests)
        for link in runner.links.values():
            await link.client.close()
    finally:
        await fake.close()


async def test_command_claims_are_exclusive(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                for i in range(20):
                    await repo.enqueue_command(
                        session_id=sid, sandbox_id=sb, kind="noop", payload={"i": i}
                    )

        async def claim(name):
            async with db() as s:
                repo = WorkbenchRepository(s)
                async with repo.transaction():
                    await repo.acquire_leases(runner_id=name, ttl_seconds=30)
                    return [
                        c["id"]
                        for c in await repo.claim_commands(claimed_by=name, limit=50)
                    ]

        # 分片：一个沙箱的命令只归持有其租约的 runner；另一个一条也拿不到
        a, b = await asyncio.gather(claim("r1"), claim("r2"))
        assert sorted([len(a), len(b)]) == [0, 21] and not set(a) & set(b)
        # 同一 runner 再拿：没有剩余
        assert await claim("r1") == [] and await claim("r2") == []
    finally:
        await fake.close()


async def test_lease_takeover_expires_pending_approvals_and_requeues_drives(db):
    """r1 崩了（租约过期）：r2 接管时作废 r1 留下的未决工具审批并发 interaction/expired，
    没有驾驶者的 Task 重新排 task.drive；r1 的旧租约再也拿不到命令。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        r1 = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        assert await r1.run_once() == 1
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": "please NEED_APPROVAL now",
                        "request_id": "req-x",
                        "by": "user",
                    },
                )
        await r1.run_once()

        async def opened():
            async with db() as s:
                return (
                    len(
                        await WorkbenchRepository(s).list_interactions(
                            session_id=sid, status="pending"
                        )
                    )
                    == 1
                )

        assert await wait_for(opened)
        # 模拟 r1 死掉：租约过期，链路不再服务
        async with db() as s:
            await s.execute(
                text(
                    "update workbench_sandbox_leases set expires_at = now() - interval '1 minute' where runner_id = 'r1'"
                )
            )
            await s.commit()
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        r2 = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r2", poll_seconds=0.05
        )
        await r2.run_once()
        async with db() as s:
            repo = WorkbenchRepository(s)
            pending = await repo.list_interactions(session_id=sid, status="pending")
            expired = await repo.list_interactions(session_id=sid, status="expired")
            events = [
                e
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "interaction/expired"
            ]
            leases = (
                (
                    await s.execute(
                        text(
                            "select runner_id from workbench_sandbox_leases where sandbox_id = :s"
                        ),
                        {"s": sb},
                    )
                )
                .scalars()
                .all()
            )
        assert pending == [] and len(expired) == 1 and len(events) == 1
        assert events[0]["payload"]["reason"] == "runner_restarted"
        assert leases == ["r2"]
        # r1 的旧租约拿不到新命令；r2 拿得到
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
                assert await repo.claim_commands(claimed_by="r1") == []
        assert await r2.run_once() >= 1
        for runner in (r1, r2):
            for link in runner.links.values():
                if link.client:
                    await link.client.close()
    finally:
        await fake.close()


async def test_takeover_requeues_running_tasks(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        from decimal import Decimal

        from app.domain.workbench.models import HandoverRequest
        from app.domain.workbench.states import TaskState

        async with db() as s:
            repo = WorkbenchRepository(s)
            led = Ledger(repo)
            task_id = (
                await led.handover(
                    HandoverRequest(
                        idempotency_key="takeover-0001",
                        session_id=sid,
                        profile_id="SMOKE",
                        original_input={"text": "q"},
                        budget_limit=Decimal("1"),
                    ),
                    owner_actor_id=OWNER,
                )
            )["task_id"]
            await led.transition(task_id, TaskState.VALIDATING)
            await led.transition(task_id, TaskState.QUEUED)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        r2 = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r2", poll_seconds=0.05
        )
        await r2.run_once()  # 首次拿租约 = 接管：QUEUED 的 Task 得到一条 task.drive
        async with db() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "select kind, payload, status from workbench_commands where kind = 'task.drive'"
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert (
            len(rows) == 1
            and rows[0]["payload"]["task_id"] == task_id
            and rows[0]["payload"]["resumed"] is True
        )
        await r2.wait_drivers(timeout=5)
        for link in r2.links.values():
            if link.client:
                await link.client.close()
    finally:
        await fake.close()


async def _started(db, fake, runner_id="r1"):
    """起线（租约归 runner_id），返回 (runner, sandbox, session, thread)。"""
    _, sb, sid = await seed(db, fake)
    runner = build_runner(
        db, publisher=RedisPublisher(None, "t"), runner_id=runner_id, poll_seconds=0.05
    )
    assert await runner.run_once() == 1
    async with db() as s:
        thread = (await WorkbenchRepository(s).get_session(sid))["thread_id"]
    return runner, sb, sid, thread


async def _task(db, sid, key, *, to=("VALIDATING", "QUEUED", "RUNNING"), attempt=False):
    from decimal import Decimal

    from app.domain.workbench.models import HandoverRequest
    from app.domain.workbench.states import AttemptState, TaskState

    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        task_id = (
            await led.handover(
                HandoverRequest(
                    idempotency_key=f"{key}-0000",
                    session_id=sid,
                    profile_id="SMOKE",
                    original_input={"text": "q"},
                    budget_limit=Decimal("1"),
                ),
                owner_actor_id=OWNER,
            )
        )["task_id"]
        for state in to:
            await led.transition(task_id, TaskState(state))
        attempt_id = None
        if attempt:
            attempt_id = (
                await led.open_attempt(
                    task_id, step_id="s1", step_version="1", reserve=Decimal("0.5")
                )
            )["attempt_id"]
            await led.attempt_transition(attempt_id, AttemptState.RUNNING)
        # 委托和对话在同一条线上（推它的程序起步时会记，这里直接记）
        await s.execute(
            text(
                "update workbench_tasks set thread_id = (select thread_id from workbench_sessions where id = :s) where id = :t"
            ),
            {"s": sid, "t": task_id},
        )
        await s.commit()
    return task_id, attempt_id


async def _restart(db, old, runner_id="r1"):
    """上一任退出（放掉租约，和 run_forever 收尾一样），同名的新进程起来。"""
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.release_leases(runner_id=runner_id)
    for link in old.links.values():
        if link.client:
            await link.client.close()
    return build_runner(
        db, publisher=RedisPublisher(None, "t"), runner_id=runner_id, poll_seconds=0.05
    )


async def _close(*runners):
    for runner in runners:
        await runner.wait_drivers(timeout=5)
        for link in runner.links.values():
            if link.client:
                await link.client.close()


async def test_a_restarted_runner_with_the_same_name_settles_a_task_nobody_drives(db):
    """runner 名字是固定配置，重启后同名。上一任死时手上的委托在跑、记着取消（旧数据：那时取消要等推它的人落实），
    又没有待办命令：新进程也拿下这个沙箱、对账、收成已取消（2026-10-19 K19 第 1 稿卡死，0034）。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        first, sb, sid, _ = await _started(db, fake)
        task_id, _ = await _task(db, sid, "restart-0001")
        async with db() as s:
            await s.execute(
                text(
                    "update workbench_tasks set cancel_requested_at = now() where id = :t"
                ),
                {"t": task_id},
            )
            await s.commit()
        second = await _restart(db, first)
        await second.run_once()  # 没有待办命令也拿下沙箱
        await second.wait_drivers(timeout=5)
        async with db() as s:
            task = await WorkbenchRepository(s).get_task(task_id)
        assert task["state"] == "CANCELLED" and sb in second.leased
        await _close(second)
    finally:
        await fake.close()


async def test_a_restarted_runner_fails_a_running_step_instead_of_running_it_twice(db):
    """在跑的一步没人推了：停下 Codex 里还在跑的那一轮，委托收成失败（平台），原因带尝试与那一轮的编号；
    不重开这一步——重开就是双跑（0034 §三.2）。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        first, sb, sid, thread = await _started(db, fake)
        task_id, attempt_id = await _task(db, sid, "restart-0002", attempt=True)
        fake.in_progress[thread] = "turn-left"
        second = await _restart(db, first)
        before = len(fake.requests)
        await second.run_once()
        await second.wait_drivers(timeout=5)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            attempt = await repo.get_attempt(attempt_id)
            events = await repo.list_task_events(task_id, types=("task/state",))
        assert task["state"] == "FAILED"
        assert (attempt["status"], attempt["failure_code"]) == ("FAILED", "platform")
        reason = events[-1]["payload"]["reason"]
        assert reason["attempt_id"] == attempt_id and reason["turn_id"] == "turn-left"
        assert (
            reason["platform"] is True
        )  # 测试结果里记出错，不记没过（K19 第 2 稿 c1）
        sent = [r for r in fake.requests[before:]]
        assert [
            r["params"]["turnId"] for r in sent if r["method"] == "turn/interrupt"
        ] == ["turn-left"]
        assert not [r for r in sent if r["method"] == "turn/start"]
        await _close(second)
    finally:
        await fake.close()


async def test_a_restarted_runner_settles_a_task_waiting_on_an_approval_of_the_old_turn(
    db,
):
    """等着 Codex 那一轮里的审批时上一任没了：旧审批作废、发 interaction/expired；那一轮没人推了，委托收尾。"""
    from app.domain.workbench.models import InteractionPrompt
    from app.domain.workbench.states import WaitingReason

    fake = FakeAppServer()
    await fake.start()
    try:
        first, sb, sid, _ = await _started(db, fake)
        task_id, _ = await _task(db, sid, "restart-0003")
        async with db() as s:
            it = await Ledger(WorkbenchRepository(s)).open_interaction(
                task_id,
                kind="tool_approval",
                prompt=InteractionPrompt(
                    title="要跑一条命令",
                    question="允许吗？",
                    subject={"request_id": "req-old"},
                ),
                waiting_reason=WaitingReason.APPROVAL,
            )
        second = await _restart(db, first)
        await second.run_once()
        await second.wait_drivers(timeout=5)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            expired = await repo.list_interactions(session_id=sid, status="expired")
            events = [
                e
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "interaction/expired"
            ]
        assert [str(x["id"]) for x in expired] == [str(it["interaction_id"])]
        assert len(events) == 1 and task["state"] == "FAILED"
        await _close(second)
    finally:
        await fake.close()


async def test_reconnecting_to_the_sandbox_expires_the_approvals_of_the_old_connection(
    db,
):
    """同一进程里到沙箱的连接断了再连：旧连接上没答的审批作废（Codex 会在新连接上重问；0034 §三.2）。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": "please NEED_APPROVAL now",
                        "request_id": "r-x",
                        "by": "user",
                    },
                )
        await runner.run_once()

        async def pending():
            async with db() as s:
                return (
                    len(
                        await WorkbenchRepository(s).list_interactions(
                            session_id=sid, status="pending"
                        )
                    )
                    == 1
                )

        assert await wait_for(pending)
        link = runner.links[sb]
        await link.client.close()
        await link.ensure_connected()
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert await repo.list_interactions(session_id=sid, status="pending") == []
            assert (
                len(await repo.list_interactions(session_id=sid, status="expired")) == 1
            )
        await _close(runner)
    finally:
        await fake.close()


async def test_reconcile_queues_one_drive_for_a_queued_task_however_many_rounds(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        task_id, _ = await _task(db, sid, "queued-0001", to=("VALIDATING", "QUEUED"))
        for _ in range(3):  # 命令没被领走时不重复排
            assert await runner._reconcile() == []
        async with db() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "select payload from workbench_commands where kind = 'task.drive' and status = 'pending'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [r["task_id"] for r in rows] == [task_id]
        await _close(runner)
    finally:
        await fake.close()


async def test_reconcile_lets_the_next_task_in_when_the_one_ahead_ended_without_letting_it(
    db,
):
    """前一位到了终态，放行那一刻没做成（例如旧版本的取消停在半路）：下一轮对账放行排队的那一件。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        ahead, _ = await _task(db, sid, "ahead-0001")
        async with db() as s:
            repo = WorkbenchRepository(s)
            project = (await repo.get_session(sid))["project_id"]
            other = (
                await SessionService(repo).start(
                    owner_actor_id=OWNER,
                    kind="expert",
                    project_id=str(project),
                    title="t2",
                )
            )["session_id"]
        behind, _ = await _task(db, other, "behind-0001", to=())
        async with db() as s:
            repo = WorkbenchRepository(s)
            queued = await repo.get_task(behind)
            assert queued["original_input"]["client_context"]["queued_behind"] == ahead
            # 前一位直接落到终态，没走放行
            await s.execute(
                text("update workbench_tasks set state = 'CANCELLED' where id = :t"),
                {"t": ahead},
            )
            await s.commit()
        await runner._reconcile()
        async with db() as s:
            repo = WorkbenchRepository(s)
            dequeued = await repo.list_task_events(behind, types=("task/dequeued",))
        assert len(dequeued) == 1
        await _close(runner)
    finally:
        await fake.close()


async def test_cancelling_a_running_task_settles_it_now_and_books_the_late_cost(db):
    """取消直接收尾（0034 §三.1）：委托与那一轮一起关、预留全退；推它的程序这一轮跑完才报的花费补记上，
    加进 budget.used 与那一轮的花费，预留不变。"""
    from decimal import Decimal

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        task_id, attempt_id = await _task(db, sid, "cancel-0001", attempt=True)
        async with db() as s:
            led = Ledger(WorkbenchRepository(s))
            done = await led.request_cancel(task_id, owner_actor_id=OWNER)
            assert done["state"] == "CANCELLED" and done["interrupt"] is True
            repo = WorkbenchRepository(s)
            attempt = await repo.get_attempt(attempt_id)
            before = (await repo.get_task(task_id))["budget"]
        assert attempt["status"] == "CANCELLED"
        assert Decimal(str(before["reserved"])) == 0
        async with db() as s:
            await Ledger(WorkbenchRepository(s)).record_late_cost(
                task_id,
                attempt_id=attempt_id,
                consumed=Decimal("0.3"),
                tokens={"input": 10},
                turn_id="turn-late",
            )
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            attempt = await repo.get_attempt(attempt_id)
            late = await repo.list_task_events(task_id, types=("attempt/late_cost",))
        assert Decimal(str(task["budget"]["used"])) == Decimal(
            str(before["used"])
        ) + Decimal("0.3")
        assert Decimal(str(task["budget"]["reserved"])) == 0
        assert attempt["status"] == "CANCELLED"
        assert Decimal(attempt["budget_consumed"]["amount"]) == Decimal("0.3")
        assert len(late) == 1 and late[0]["payload"]["turn_id"] == "turn-late"
        await _close(runner)
    finally:
        await fake.close()


async def test_a_crash_of_a_task_that_was_being_cancelled_ends_it_as_cancelled(db):
    from app.application.workbench.advisor import Advisor
    from app.bootstrap.workbench import workbench_stores

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        task_id, attempt_id = await _task(db, sid, "crash-0001", attempt=True)
        async with db() as s:
            await s.execute(
                text(
                    "update workbench_tasks set cancel_requested_at = now() where id = :t"
                ),
                {"t": task_id},
            )
            await s.commit()
        await Advisor(workbench_stores(db), None).fail_after_crash(task_id, "boom")  # type: ignore[arg-type]
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            attempt = await repo.get_attempt(attempt_id)
        assert task["state"] == "CANCELLED"
        assert (attempt["status"], attempt["failure_code"]) == (
            "CANCELLED",
            "cancelled",
        )
        await _close(runner)
    finally:
        await fake.close()


async def test_a_crash_fails_the_task_as_a_platform_failure(db):
    """推它的程序崩了：委托记失败，原因标平台（测试结果里记出错，不记没过）。"""
    from app.application.workbench.advisor import Advisor
    from app.bootstrap.workbench import workbench_stores

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        task_id, attempt_id = await _task(db, sid, "crash-0002", attempt=True)
        await Advisor(workbench_stores(db), None).fail_after_crash(task_id, "boom")  # type: ignore[arg-type]
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            attempt = await repo.get_attempt(attempt_id)
            events = await repo.list_task_events(task_id, types=("task/state",))
        assert task["state"] == "FAILED"
        assert attempt["failure_code"] == "platform"
        assert events[-1]["payload"]["reason"] == {"error": "boom", "platform": True}
        await _close(runner)
    finally:
        await fake.close()


async def _second_session(db, sid):
    async with db() as s:
        repo = WorkbenchRepository(s)
        project = (await repo.get_session(sid))["project_id"]
        return (
            await SessionService(repo).start(
                owner_actor_id=OWNER, kind="expert", project_id=str(project), title="t"
            )
        )["session_id"]


async def _other_project(db, sid, name):
    """同一台机器、同一个沙箱上另一个项目里的对话（一个项目同时只能有一件活跃委托）。"""
    async with db() as s:
        repo = WorkbenchRepository(s)
        talk = await repo.get_session(sid)
        return (
            await SessionService(repo).create(
                owner_actor_id=OWNER,
                environment_id=str(talk["environment_id"]),
                sandbox_id=str(talk["sandbox_id"]),
                project_root=f"/home/u/research/{name}",
            )
        )["session_id"]


async def _dequeued(db, task_id):
    async with db() as s:
        return len(
            await WorkbenchRepository(s).list_task_events(
                task_id, types=("task/dequeued",)
            )
        )


async def test_cancelling_a_queued_task_does_not_let_the_next_run_beside_the_running_one(
    db,
):
    """A 在跑，B、C 排队：取消 B（例如排队超时），C 不能放进来和 A 同跑（0034 评审）。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        a, _ = await _task(db, sid, "q-a", attempt=True)
        b, _ = await _task(db, await _second_session(db, sid), "q-b", to=())
        c, _ = await _task(db, await _second_session(db, sid), "q-c", to=())
        async with db() as s:
            await Ledger(WorkbenchRepository(s)).request_cancel(b, owner_actor_id=OWNER)
        await runner._reconcile()
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert (await repo.get_task(a))["state"] == "RUNNING"
            assert (await repo.get_task(c))["state"] == "RECEIVED"
        assert await _dequeued(db, c) == 0
        await _close(runner)
    finally:
        await fake.close()


async def test_a_cancelled_turn_holds_the_project_until_it_ends(db):
    """取消在跑的委托：账马上收，但 Codex 那一轮确认结束前不放行下一位；推它的程序这一轮跑完（补记花费）才放行。"""
    from decimal import Decimal

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        a, attempt = await _task(db, sid, "hold-a", attempt=True)
        b, _ = await _task(db, await _second_session(db, sid), "hold-b", to=())
        async with db() as s:
            await Ledger(WorkbenchRepository(s)).request_cancel(a, owner_actor_id=OWNER)
        await runner._reconcile()
        assert await _dequeued(db, b) == 0  # 那一轮还没确认结束
        async with db() as s:
            led = Ledger(WorkbenchRepository(s))
            await led.record_late_cost(
                a, attempt_id=attempt, consumed=Decimal("0.2"), tokens={}, turn_id="t1"
            )
            assert await led.release_turn(a) is True
        assert await _dequeued(db, b) == 1
        await _close(runner)
    finally:
        await fake.close()


async def test_the_same_late_cost_is_booked_once(db):
    from decimal import Decimal

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        a, attempt = await _task(db, sid, "dup-a", attempt=True)
        async with db() as s:
            await Ledger(WorkbenchRepository(s)).request_cancel(a, owner_actor_id=OWNER)
        for _ in range(2):  # 重复回报、结算后重试
            async with db() as s:
                await Ledger(WorkbenchRepository(s)).record_late_cost(
                    a,
                    attempt_id=attempt,
                    consumed=Decimal("0.2"),
                    tokens={},
                    turn_id="t1",
                )
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(a)
            late = await repo.list_task_events(a, types=("attempt/late_cost",))
        assert Decimal(str(task["budget"]["used"])) == Decimal("0.2") and len(late) == 1
        await _close(runner)
    finally:
        await fake.close()


async def test_tasks_left_before_their_first_turn_are_driven_again(db):
    """受理了还没发出命令、核验中进程没了：都还没开工，接着推（不会双跑）；正常排在项目队列里的、
    等用户回答开步前提问的不动。"""
    from app.domain.workbench.models import InteractionPrompt
    from app.domain.workbench.states import WaitingReason

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        received, _ = await _task(db, sid, "left-r", to=())
        validating, _ = await _task(
            db, await _other_project(db, sid, "p2"), "left-v", to=("VALIDATING",)
        )
        asking, _ = await _task(
            db,
            await _other_project(db, sid, "p3"),
            "left-w",
            to=("VALIDATING", "QUEUED"),
        )
        async with db() as s:
            await Ledger(WorkbenchRepository(s)).open_interaction(
                asking,
                kind="input",
                prompt=InteractionPrompt(title="开步前", question="看哪几年？"),
                waiting_reason=WaitingReason.INPUT,
            )
            # 交接时排的那条 task.drive 丢了（进程在发出前没了）
            await s.execute(
                text("delete from workbench_commands where kind = 'task.drive'")
            )
            await s.commit()
        assert await runner._reconcile() == []
        async with db() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "select payload from workbench_commands where kind = 'task.drive' and status = 'pending'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            asking_state = (await WorkbenchRepository(s).get_task(asking))["state"]
        assert sorted(r["task_id"] for r in rows) == sorted([received, validating])
        assert asking_state == "WAITING"
        await _close(runner)
    finally:
        await fake.close()


async def test_a_turn_that_will_not_stop_holds_the_project_then_lets_go_and_says_cost_unknown(
    db,
):
    """接手时停不下残留那一轮：委托照样收尾，但不放行下一位，不冒险并跑；等过 TURN_HOLD_SECONDS 才放行，
    记「费用待核对」（0034 §三.1）。"""
    from app.domain.workbench.states import TURN_HOLD_SECONDS

    fake = FakeAppServer()
    await fake.start()
    try:
        first, sb, sid, thread = await _started(db, fake)
        a, _ = await _task(db, sid, "stuck-a", attempt=True)
        b, _ = await _task(db, await _second_session(db, sid), "stuck-b", to=())
        fake.in_progress[thread] = "turn-stuck"
        fake.stubborn = True
        second = await _restart(db, first)
        second.stop_confirm_seconds = 0.3
        await second.run_once()
        await second.wait_drivers(timeout=5)
        async with db() as s:
            assert (await WorkbenchRepository(s).get_task(a))["state"] == "FAILED"
        await second._reconcile()
        assert await _dequeued(db, b) == 0
        async with db() as s:  # 时限过了
            await s.execute(
                text(
                    "update workbench_tasks set original_input = jsonb_set(original_input, "
                    "'{client_context,turn_open,since}', to_jsonb((now() - make_interval(secs => :h))::text)) "
                    "where id = :t"
                ),
                {"t": a, "h": TURN_HOLD_SECONDS + 1},
            )
            await s.commit()
        await second._reconcile()
        assert await _dequeued(db, b) == 1
        async with db() as s:
            expired = await WorkbenchRepository(s).list_task_events(
                a, types=("turn/hold_expired",)
            )
        assert expired[0]["payload"]["cost"] == "unknown"
        await _close(second)
    finally:
        await fake.close()


async def test_one_bad_task_does_not_stop_the_round_for_the_others(db, monkeypatch):
    """对账时一件出错只跳过这一件，别的照推；连着出错的轮数记下来（连着 3 轮记错误日志）。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        bad, _ = await _task(db, sid, "bad-1", to=("VALIDATING", "QUEUED"))
        good, _ = await _task(
            db,
            await _other_project(db, sid, "p2"),
            "good-1",
            to=("VALIDATING", "QUEUED"),
        )
        async with db() as s:
            await s.execute(
                text("delete from workbench_commands where kind = 'task.drive'")
            )
            await s.commit()
        real = WorkbenchRepository.enqueue_command

        async def flaky(self, **kw):
            if kw.get("payload", {}).get("task_id") == bad:
                raise RuntimeError("boom")
            return await real(self, **kw)

        monkeypatch.setattr(WorkbenchRepository, "enqueue_command", flaky)
        await runner._reconcile()
        async with db() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "select payload from workbench_commands where kind = 'task.drive' and status = 'pending'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [r["task_id"] for r in rows] == [good]
        assert runner.reconcile_failures == 1
        await _close(runner)
    finally:
        await fake.close()


async def test_time_spent_waiting_for_an_answer_does_not_count_against_the_step(
    db, monkeypatch
):
    """专家停下来问人时，这一步的钟停着：答得比时限慢，这一步也不按超时判失败（2026-10-19 K19 第 2 稿：
    所有者思考作答的几分钟算进了 15 分钟，这一步超时，问题挂着、测试卡死）。"""
    from app.application.workbench import advisor as advisor_module
    from app.domain.workbench.states import AttemptState

    monkeypatch.setattr(advisor_module, "STEP_TIMEOUT_SECONDS", 0.5)
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        task_id = await _advisor_task(db, sid, "NEED_APPROVAL", "slow-answer-1")
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        await _pump(runner)

        async def opened():
            return len(await _pending(db, sid, "tool_approval")) == 1

        assert await wait_for(opened)
        await asyncio.sleep(1.2)  # 比这一步的时限（0.5 秒）慢得多才答
        [it] = await _pending(db, sid, "tool_approval")
        await _answer(db, sb, sid, it, {"decision": "accept"})
        await _pump(runner)
        await runner.wait_drivers(timeout=5)
        async with db() as s:
            repo = WorkbenchRepository(s)
            first = (await repo.list_attempts(task_id))[0]
            rejected = await repo.list_task_events(task_id, types=("step/rejected",))
        # 那一轮等到了答复、自己做完了（假 app-server 交回的不合这一步的验收，那是另一回事），
        # 不是被时限停下：没有「timeout」这一条
        assert first["status"] != AttemptState.CANCELLED
        failures = [f for r in rejected for f in r["payload"].get("failures") or []]
        assert rejected and not any(f.startswith("timeout") for f in failures)
        await _close(runner)
    finally:
        await fake.close()


async def test_a_question_left_open_when_its_turn_ends_is_dropped_and_the_task_moves_on(
    db,
):
    """那一轮结束了（例如真到了时限），它里面还挂着的问题作废、委托回到在跑，按这一轮的结果接着走；
    之后这条线上的新一轮不会被那个旧问题的善后误停。"""
    from app.domain.workbench.models import InteractionPrompt
    from app.domain.workbench.states import WaitingReason

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sb, sid, _ = await _started(db, fake)
        task_id, _ = await _task(db, sid, "turn-ended-1", attempt=True)
        async with db() as s:
            led = Ledger(WorkbenchRepository(s))
            await led.open_interaction(
                task_id,
                kind="question",
                prompt=InteractionPrompt(
                    title="Codex 想问你",
                    question="印刷页读不到，怎么办？",
                    subject={"request_id": "req-q"},
                ),
                waiting_reason=WaitingReason.INPUT,
            )
            assert await led.end_turn_waits(task_id) is True
            assert await led.end_turn_waits(task_id) is False  # 只做一次
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            left = await repo.list_interactions(session_id=sid, status="pending")
        assert task["state"] == "RUNNING" and task["active_interaction_id"] is None
        assert left == []
        # 委托级的等待（开步前的提问，没有 request_id）不动
        other, _ = await _task(
            db, await _other_project(db, sid, "p2"), "turn-ended-2", attempt=True
        )
        async with db() as s:
            led = Ledger(WorkbenchRepository(s))
            await led.open_interaction(
                other,
                kind="input",
                prompt=InteractionPrompt(title="开步前", question="看哪几年？"),
                waiting_reason=WaitingReason.INPUT,
            )
            assert await led.end_turn_waits(other) is False
        await _close(runner)
    finally:
        await fake.close()


@pytest.mark.parametrize("ref,expected", [("inline:abc", "abc")])
def test_token_ref(ref, expected):
    from app.infrastructure.workbench.app_server_client import resolve_token_ref

    assert resolve_token_ref(ref) == expected
    with pytest.raises(RuntimeError):
        resolve_token_ref("vault:nope")


async def test_mcp_elicitation_is_accepted_with_schema_defaults(db):
    """Codex 调 MCP 工具前的 user-verification 询问：表单式按 schema 默认值答 accept，工具调用得以完成。"""
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        assert await runner.run_once() == 1
        async with db() as s:
            repo = WorkbenchRepository(s)
            await SessionService(repo).record_user_turn_requested(
                sid, owner_actor_id=OWNER, text="NEED_ELICITATION", request_id="req-e"
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": "NEED_ELICITATION",
                        "request_id": "req-e",
                        "by": "user",
                    },
                )
        assert await runner.run_once() == 1

        async def turn_done():
            async with db() as s:
                return any(
                    e["type"] == "turn/completed"
                    for e in await WorkbenchRepository(s).list_events(session_id=sid)
                )

        assert await wait_for(turn_done)
        assert fake.elicitations == [
            {"action": "accept", "content": {"confirm": True, "note": "ok"}}
        ]
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        tool = next(
            e
            for e in events
            if e["type"] == "item/completed"
            and e["payload"]["item"].get("type") == "mcpToolCall"
        )
        assert tool["payload"]["item"]["status"] == "completed"
        for link in runner.links.values():
            await link.client.close()
    finally:
        await fake.close()


async def test_reconnect_reads_the_current_sandbox_token(db):
    """回收后重新拉起会换沙箱的能力令牌；runner 重连时要用库里的新令牌，而不是缓存里的旧令牌。"""
    fake = FakeAppServer()
    await fake.start()
    runner = build_runner(
        db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
    )
    try:
        _, sb, sid = await seed(db, fake)
        assert await runner.run_once() == 1  # session.start_thread，用旧令牌连上
        assert fake.auth_headers[-1] == f"Bearer {TOKEN}"
        # 沙箱被回收又拉起：连接断了，库里的令牌换了
        client = runner.links[sb].client
        assert client is not None
        await client.close()
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await s.execute(
                    text(
                        "update workbench_sandboxes set token_ref = :r where id = :id"
                    ),
                    {"r": "inline:cap-token-after-reprovision", "id": sb},
                )
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": "hello again",
                        "request_id": "req-2",
                        "by": "user",
                    },
                )
        await runner.run_once()
        assert fake.auth_headers[-1] == "Bearer cap-token-after-reprovision"
        assert await turn_completed(db, sid)
    finally:
        for link in runner.links.values():
            if link.client is not None:
                await link.client.close()
        await fake.close()


async def _reconnect_after_sandbox_restart(db, fake, *, wipe: bool):
    """公共部分：起线程 → 沙箱换了进程（连接断、令牌换）→ 再发一个 turn。返回 (runner, sb, sid, 旧线程号)。"""
    runner = build_runner(
        db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
    )
    _, sb, sid = await seed(db, fake)
    assert await runner.run_once() == 1
    async with db() as s:
        old_thread = (await WorkbenchRepository(s).get_session(sid))["thread_id"]
    assert old_thread in fake.live
    client = runner.links[sb].client
    assert client is not None
    await client.close()
    fake.wipe() if wipe else fake.restart()
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sb,
                kind="turn.start",
                payload={"text": "hello again", "request_id": "req-2", "by": "user"},
            )
    await runner.run_once()
    assert await turn_completed(db, sid)
    return runner, sb, sid, old_thread


async def test_reconnect_resumes_the_thread_on_the_new_app_server(db):
    """回收再拉起：新进程内存里没有线程，但 rollout 在持久卷上；runner 要先 thread/resume 再 turn/start，线程号不变。"""
    fake = FakeAppServer()
    await fake.start()
    runner = None
    try:
        runner, sb, sid, old_thread = await _reconnect_after_sandbox_restart(
            db, fake, wipe=False
        )
        methods = [
            (r["method"], (r.get("params") or {}).get("threadId"))
            for r in fake.requests
            if r["method"] in ("thread/resume", "turn/start", "thread/start")
        ]
        assert methods[-2:] == [
            ("thread/resume", old_thread),
            ("turn/start", old_thread),
        ]
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert (await repo.get_session(sid))["thread_id"] == old_thread
            events = await repo.list_events(session_id=sid)
        accepted = [e for e in events if e["type"] == "turn/accepted"]
        assert [e["payload"]["request_id"] for e in accepted] == ["req-2"]
        assert not any(e["type"] == "command/failed" for e in events)
    finally:
        if runner is not None:
            for link in runner.links.values():
                if link.client is not None:
                    await link.client.close()
        await fake.close()


async def test_reconnect_starts_a_new_thread_when_the_rollout_is_gone(db):
    """连 rollout 都没了（换过持久卷）：resume 报 not found，runner 另起一条线程、换掉会话记的线程号，并在时间线里留下痕迹。"""
    fake = FakeAppServer()
    await fake.start()
    runner = None
    try:
        runner, sb, sid, old_thread = await _reconnect_after_sandbox_restart(
            db, fake, wipe=True
        )
        async with db() as s:
            repo = WorkbenchRepository(s)
            new_thread = (await repo.get_session(sid))["thread_id"]
            events = await repo.list_events(session_id=sid)
        assert new_thread and new_thread != old_thread
        started = [e for e in events if e["type"] == "session/thread_started"]
        assert started[-1]["payload"]["replaced_thread_id"] == old_thread
        assert started[-1]["payload"]["thread_id"] == new_thread
        accepted = [e for e in events if e["type"] == "turn/accepted"]
        assert [e["payload"]["request_id"] for e in accepted] == ["req-2"]
        turn_threads = [
            r["params"]["threadId"]
            for r in fake.requests
            if r["method"] == "turn/start"
        ]
        assert turn_threads[-1] == new_thread
    finally:
        if runner is not None:
            for link in runner.links.values():
                if link.client is not None:
                    await link.client.close()
        await fake.close()


# ---------------- 工具级审批重做（approval.md；账 77、79）----------------


async def _advisor_task(db, sid, text_, key):
    """请专家（SMOKE）并把 Task 排到 QUEUED：问题文字里带触发词，专家的第一轮就会碰到审批。"""
    from decimal import Decimal

    from app.domain.workbench.models import HandoverRequest
    from app.domain.workbench.states import TaskState

    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        task_id = (
            await led.handover(
                HandoverRequest(
                    idempotency_key=f"{key}-0000",
                    session_id=sid,
                    profile_id="SMOKE",
                    original_input={"text": text_},
                    budget_limit=Decimal("5"),
                ),
                owner_actor_id=OWNER,
            )
        )["task_id"]
        await led.transition(task_id, TaskState.VALIDATING)
        await led.transition(task_id, TaskState.QUEUED)
    return task_id


async def _pump(runner, times=6):
    for _ in range(times):
        await runner.run_once()
        await asyncio.sleep(0.05)


async def _pending(db, sid, kind):
    async with db() as s:
        return [
            it
            for it in await WorkbenchRepository(s).list_interactions(
                session_id=sid, status="pending"
            )
            if it["kind"] == kind
        ]


async def _answer(db, sb, sid, it, response):
    """网页上的回答：消费 Interaction，再排一条 approval.respond 命令（与路由层做的一样）。"""
    async with db() as s:
        repo = WorkbenchRepository(s)
        token = next(
            e["payload"]["token"]
            for e in await repo.list_events(session_id=sid)
            if e["type"] == "interaction/opened"
            and e["payload"]["interaction_id"] == str(it["id"])
        )
        await Ledger(repo).respond_interaction(
            str(it["id"]), token=token, response=response, owner_actor_id=OWNER
        )
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sb,
                kind="approval.respond",
                payload={
                    "request_id": it["prompt"]["subject"]["request_id"],
                    "decision": response.get("decision"),
                    "response": response,
                },
            )


async def _approval_rows(db, sid):
    async with db() as s:
        r = await s.execute(
            text(
                "select decision, source, wheel, step_id from workbench_approval_log where session_id = :s order by created_at, id"
            ),
            {"s": sid},
        )
        return [tuple(x) for x in r.all()]


async def test_advisor_policy_lets_in_project_file_changes_through_without_asking(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        await _advisor_task(db, sid, "NEED_FILE_APPROVAL", "pol-0001")
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        await _pump(runner)

        async def decided():
            return any(
                (d.get("result") or {}).get("decision") == "accept"
                for d in fake.approval_decisions
            )

        assert await wait_for(decided)
        assert await _pending(db, sid, "tool_approval") == []
        rows = await _approval_rows(db, sid)
        assert rows[0] == ("accept", "policy", "advisor", "smoke-1") or rows[0][:3] == (
            "accept",
            "policy",
            "advisor",
        )
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        assert any(e["type"] == "approval/policy" for e in events)
        await runner.wait_drivers(timeout=5)
        for link in runner.links.values():
            if link.client:
                await link.client.close()
    finally:
        await fake.close()


async def test_advisor_command_asks_with_four_buttons_and_a_task_grant_covers_the_next(
    db,
):
    from app.domain.workbench.states import TaskState, WaitingReason

    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        task_id = await _advisor_task(db, sid, "NEED_APPROVAL_TWICE", "grant-0001")
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        await _pump(runner)

        async def opened():
            return len(await _pending(db, sid, "tool_approval")) == 1

        assert await wait_for(opened)
        it = (await _pending(db, sid, "tool_approval"))[0]
        assert str(it["task_id"]) == task_id
        assert [o["id"] for o in it["prompt"]["options"]] == [
            "accept",
            "accept_task",
            "decline",
            "cancel",
        ]
        subject = it["prompt"]["subject"]
        assert subject["wheel"] == "advisor" and subject["step"]["index"] == 1
        assert subject["after_decline"] is False
        async with db() as s:
            task = await WorkbenchRepository(s).get_task(task_id)
        assert task["state"] == TaskState.WAITING
        assert task["waiting_reason"] == WaitingReason.APPROVAL

        await _answer(db, sb, sid, it, {"decision": "accept_task"})
        await _pump(runner)

        async def both():
            return (
                sum(
                    1
                    for d in fake.approval_decisions
                    if (d.get("result") or {}).get("decision") == "accept"
                )
                >= 2
            )

        assert await wait_for(both)
        # 第二次同样的命令没有再问人：由工作台记的任务内授权放行（SMOKE 验收不过会返工，返工轮里的
        # 同样命令也都靠这条授权放行，所以后面可能不止一行 task_grant）
        assert await _pending(db, sid, "tool_approval") == []
        rows = await _approval_rows(db, sid)
        assert [r[:2] for r in rows[:2]] == [
            ("accept", "user"),
            ("accept", "task_grant"),
        ]
        assert all(r[:2] == ("accept", "task_grant") for r in rows[2:])
        assert all(r[2] == "advisor" and r[3] for r in rows)
        async with db() as s:
            r = await s.execute(
                text(
                    "select category, command_prefix from workbench_tool_grants where task_id = :t"
                ),
                {"t": task_id},
            )
            grants = [tuple(x) for x in r.all()]
        assert grants == [("command", ["touch", "/home/u/x"])]
        await runner.wait_drivers(timeout=5)
        for link in runner.links.values():
            if link.client:
                await link.client.close()
    finally:
        await fake.close()


async def test_after_a_decline_even_allowed_changes_ask_and_say_so(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        await _advisor_task(db, sid, "NEED_APPROVAL_THEN_FILE", "decl-0001")
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        await _pump(runner)

        async def opened():
            return len(await _pending(db, sid, "tool_approval")) == 1

        assert await wait_for(opened)
        first = (await _pending(db, sid, "tool_approval"))[0]
        await _answer(db, sb, sid, first, {"decision": "decline"})
        await _pump(runner)

        async def second_opened():
            pend = await _pending(db, sid, "tool_approval")
            return len(pend) == 1 and str(pend[0]["id"]) != str(first["id"])

        # 项目内改文件本来是策略放行的；刚拒绝过，就要问人，并标出来
        assert await wait_for(second_opened)
        second = (await _pending(db, sid, "tool_approval"))[0]
        assert second["prompt"]["subject"]["after_decline"] is True
        assert second["prompt"]["subject"]["category"] == "file_change_in_project"
        await _answer(db, sb, sid, second, {"decision": "accept"})
        await _pump(runner)
        rows = await _approval_rows(db, sid)
        assert [r[:2] for r in rows] == [("decline", "user"), ("accept", "user")]
        await runner.wait_drivers(timeout=5)
        for link in runner.links.values():
            if link.client:
                await link.client.close()
    finally:
        await fake.close()


async def test_stop_interrupts_the_turn_and_parks_the_step_for_the_user(db):
    from app.domain.workbench.states import TaskState, WaitingReason

    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        task_id = await _advisor_task(db, sid, "NEED_APPROVAL", "stop-0001")
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        await _pump(runner)

        async def opened():
            return len(await _pending(db, sid, "tool_approval")) == 1

        assert await wait_for(opened)
        it = (await _pending(db, sid, "tool_approval"))[0]
        await _answer(
            db,
            sb,
            sid,
            it,
            {"decision": "cancel", "answer": {"text": "先别碰 x，改用只读的方式"}},
        )
        await _pump(runner)

        async def parked():
            async with db() as s:
                task = await WorkbenchRepository(s).get_task(task_id)
            return (
                task["state"] == TaskState.WAITING
                and task["waiting_reason"] == WaitingReason.INPUT
            )

        assert await wait_for(parked)
        assert any(
            (d.get("result") or {}).get("decision") == "cancel"
            for d in fake.approval_decisions
        )
        inputs = await _pending(db, sid, "input")
        assert len(inputs) == 1 and "停下" in inputs[0]["prompt"]["title"]
        assert inputs[0]["prompt"]["subject"]["note"] == "先别碰 x，改用只读的方式"
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        assert any(e["type"] == "step/stopped" for e in events)
        rows = await _approval_rows(db, sid)
        assert rows[0][:2] == ("cancel", "user")
        await runner.wait_drivers(timeout=5)
        for link in runner.links.values():
            if link.client:
                await link.client.close()
    finally:
        await fake.close()


async def test_cancelling_while_the_step_waits_for_approval_stops_the_turn_now(db):
    """2026-10-14 所有者：专家页的「停止」不管用。卡在等人批准时取消：委托直接收成已取消，
    但那一轮还在 Codex 里等批准，要到 15 分钟时限才停；顾问随后再收一次「已取消」还会出错。
    现在取消也发中断，runner 先把这条线上等着的那一问按「停下」答掉，那一轮马上收住。"""
    from app.domain.workbench.states import AttemptState, TaskState

    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        task_id = await _advisor_task(db, sid, "NEED_APPROVAL", "cancel-wait-0001")
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid, sandbox_id=sb, kind="noop", payload={}
                )
        await _pump(runner)

        async def opened():
            return len(await _pending(db, sid, "tool_approval")) == 1

        assert await wait_for(opened)
        # 和取消路由做的一样：先记取消，要中断就排一条 turn.interrupt
        async with db() as s:
            repo = WorkbenchRepository(s)
            result = await Ledger(repo).request_cancel(task_id, owner_actor_id=OWNER)
            assert (
                result["state"] == TaskState.CANCELLED and result["interrupt"] is True
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.interrupt",
                    payload={"by": "user", "task_id": task_id},
                )
        await _pump(runner)

        async def settled():
            async with db() as s:
                attempts = await WorkbenchRepository(s).list_attempts(task_id)
            return not runner.driving and all(
                a["status"] == AttemptState.CANCELLED for a in attempts
            )

        assert await wait_for(settled, timeout=5)
        # Codex 收到的是「停下」，不是一直等
        assert any(
            (d.get("result") or {}).get("decision") == "cancel"
            for d in fake.approval_decisions
        )
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(task_id)
            events = await repo.list_events(session_id=sid)
        assert task["state"] == TaskState.CANCELLED
        # 中断那条命令本身不报错（放掉之后 Codex 已经自己收住了这一轮）
        assert not any(
            e["type"] == "command/failed" and e["payload"]["kind"] == "turn.interrupt"
            for e in events
        )
        stop = [e for e in events if e["type"] == "turn/stop_requested"]
        assert len(stop) == 1
        for link in runner.links.values():
            assert not link.pending_approvals and not link.pending_threads
            if link.client:
                await link.client.close()
    finally:
        await fake.close()


async def test_codex_question_becomes_a_card_and_the_answer_goes_back(db):
    fake = FakeAppServer()
    await fake.start()
    try:
        _, sb, sid = await seed(db, fake)
        runner = build_runner(
            db, publisher=RedisPublisher(None, "t"), runner_id="r1", poll_seconds=0.05
        )
        await runner.run_once()
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": "NEED_QUESTION please",
                        "request_id": "req-q",
                        "by": "user",
                    },
                )
        await _pump(runner, 3)

        async def opened():
            return len(await _pending(db, sid, "question")) == 1

        assert await wait_for(opened)
        it = (await _pending(db, sid, "question"))[0]
        assert it["task_id"] is None
        q = it["prompt"]["subject"]["questions"][0]
        assert q["id"] == "q1" and q["is_other"] is True and len(q["options"]) == 2
        await _answer(
            db,
            sb,
            sid,
            it,
            {"decision": "answer", "answer": {"answers": {"q1": "2023"}}},
        )
        await _pump(runner)

        async def answered():
            return len(fake.question_answers) == 1

        assert await wait_for(answered)
        assert fake.question_answers[0] == {"answers": {"q1": {"answers": ["2023"]}}}
        async with db() as s:
            events = await WorkbenchRepository(s).list_events(session_id=sid)
        assert any(e["type"] == "question/answered" for e in events)
        for link in runner.links.values():
            if link.client:
                await link.client.close()
    finally:
        await fake.close()
