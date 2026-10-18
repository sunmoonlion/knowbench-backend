"""顾问（工作流解释器）：假 app-server 按脚本回复，真数据库。五条路径：顺利、返工、返工用尽交人、预算硬停、取消；外加无专家包拒绝与定位拦截。"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

from test_workbench_ledger_db import OWNER  # noqa: F401
from test_workbench_ledger_db import db as db
from usage_support import MODEL, UsageMeter
from websockets.asyncio.server import serve

from app.application.workbench.acceptance import judge
from app.application.workbench.ledger import Ledger
from app.application.workbench.session_service import SessionService
from app.bootstrap.workbench import build_runner
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.packs import FIN_FORECAST, SMOKE, AcceptanceRule, find_pack
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository

PLAN = json.dumps({"plan": ["read README", "answer"], "assumptions": []})
ANSWER = json.dumps(
    {
        "answer": "The project has one README.",
        "citations": ["README.md"],
        "conclusion": "",
    }
)


class ScriptedAppServer:
    """每个 turn/start 消费一条脚本回复；脚本用完就回显。用量每轮固定 1000 token（默认价 0.01/1k → 每 turn 0.01）。"""

    def __init__(self, replies: list[str] | None = None, tokens_per_turn: int = 1000):
        self.replies = list(replies or [])
        self.tokens = tokens_per_turn
        self.meter = UsageMeter()
        self.turn_inputs: list[str] = []
        self.port = 0
        self.server = None

    async def start(self):
        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                if "method" in msg and "id" in msg:
                    asyncio.create_task(self.handle(ws, msg))

        self.server = await serve(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, ws, msg):
        m, p, rid = msg["method"], msg.get("params") or {}, msg["id"]
        send = lambda o: ws.send(json.dumps(o))  # noqa: E731
        if m == "initialize":
            await send({"id": rid, "result": {}})
        elif m == "thread/start":
            tid = f"thread-{uuid.uuid4().hex[:8]}"
            await send({"id": rid, "result": {"thread": {"id": tid}, "model": MODEL}})
        elif m == "turn/start":
            tid, turn_id = p["threadId"], f"turn-{uuid.uuid4().hex[:6]}"
            text = p["input"][0]["text"]
            self.turn_inputs.append(text)
            await send(
                {"id": rid, "result": {"turn": {"id": turn_id, "threadId": tid}}}
            )
            reply = self.replies.pop(0) if self.replies else f"echo: {text[:40]}"
            await asyncio.sleep(0.01)
            await send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": tid,
                        "turnId": turn_id,
                        "item": {"type": "agentMessage", "id": "m", "text": reply},
                    },
                }
            )
            await send(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": tid,
                        "turnId": turn_id,
                        "tokenUsage": self.meter.report(tid, self.tokens),
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
        else:
            await send({"id": rid, "result": {}})

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


async def seed(db, fake, *, profile="SMOKE", budget="10"):
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            env = await repo.register_environment(
                owner_actor_id=OWNER,
                name="pc",
                agent_version="0.1.0",
                codex_version="0.155.1",
                roots=["/home/u/research"],
                ceiling={},
            )
            sb = await repo.register_sandbox(
                owner_actor_id=OWNER,
                app_server_url=f"ws://127.0.0.1:{fake.port}",
                token_ref="inline:t",
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
    runner = build_runner(
        db, publisher=RedisPublisher(None, "t"), runner_id="r", poll_seconds=0.05
    )
    await runner.run_once()
    async with db() as s:
        repo = WorkbenchRepository(s)
        t = (
            await Ledger(repo).handover(
                HandoverRequest(
                    idempotency_key=f"k-{uuid.uuid4().hex[:12]}",
                    session_id=sid,
                    profile_id=profile,
                    original_input={"text": "What is in this project?"},
                    budget_limit=Decimal(budget),
                ),
                owner_actor_id=OWNER,
            )
        )["task_id"]
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid, sandbox_id=sb, kind="task.drive", payload={"task_id": t}
            )
    return runner, sid, sb, t


async def drive(runner):
    await runner.run_once()
    await runner.wait_drivers(timeout=30)


async def close(runner):
    for link in runner.links.values():
        if link.client:
            await link.client.close()


async def test_happy_path_two_steps(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t)
            assert task["state"] == TaskState.SUCCEEDED and task["current_step"] == 2
            arts = {(a["name"], a["version"]) for a in await repo.list_artifacts(t)}
            assert arts == {("plan", 1), ("answer", 1), ("handback", 1)}
            assert Decimal(task["budget"]["used"]) == Decimal("0.02")
            assert Decimal(task["budget"]["reserved"]) == 0
            assert (await repo.get_session(sid))["wheel"] == "user"
            types = [e["type"] for e in await repo.list_events(session_id=sid)]
            assert (
                types.count("step/started") == 2
                and types.count("step/accepted") == 2
                and "wheel/return" in types
            )
            back = [
                e
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "wheel/return"
            ]
            # 机制烟测写成稿文件：交回那一行照旧指向「工作底稿」
            assert back[-1]["payload"]["writes_files"] is True
            attempts = await repo.list_attempts(t)
            assert [a["status"] for a in attempts] == ["COMPLETED", "COMPLETED"]
            hb = await repo.get_artifact(task_id=t, name="handback")
            assert set(hb["content"]) >= {
                "did",
                "verified",
                "unknown",
                "workspace_restore",
            }
        # 第二步的 turn 输入带了第一步的 Artifact
        assert (
            "plan (v1)" in fake.turn_inputs[1] and "read README" in fake.turn_inputs[1]
        )
        await close(runner)
    finally:
        await fake.close()


async def test_rework_once_then_succeed(db):
    fake = ScriptedAppServer(["sorry, no json here", PLAN, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert (await repo.get_task(t))["state"] == TaskState.SUCCEEDED
            attempts = await repo.list_attempts(t)
            assert [
                (a["step_id"], a["status"], a["failure_code"]) for a in attempts
            ] == [
                ("plan", "FAILED", "acceptance"),
                ("plan", "COMPLETED", None),
                ("answer", "COMPLETED", None),
            ]
            assert (await repo.get_artifact(task_id=t, name="plan"))["version"] == 1
            assert Decimal((await repo.get_task(t))["budget"]["used"]) == Decimal(
                "0.03"
            )
        assert "rework #1" in fake.turn_inputs[1]
        # 上一次没过的各条带进重做的提示（所有者 2026-10-17）：这里是「不是 JSON」
        assert (
            "What failed last time (fix these):\n- json_object:" in fake.turn_inputs[1]
        )
        assert "What failed last time" not in fake.turn_inputs[0]
        await close(runner)
    finally:
        await fake.close()


async def test_reworks_exhausted_hands_to_human_then_resumes(db):
    bad = json.dumps({"answer": "", "citations": []})
    fake = ScriptedAppServer([PLAN, bad, bad, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t)
            assert (
                task["state"] == TaskState.WAITING and task["waiting_reason"] == "INPUT"
            )
            it = (await repo.list_interactions(session_id=sid, status="pending"))[0]
            assert (
                it["kind"] == "input" and it["prompt"]["subject"]["step_id"] == "answer"
            )
            token = next(
                e["payload"]["token"]
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "interaction/opened"
            )
            r = await Ledger(repo).respond_interaction(
                str(it["id"]),
                token=token,
                response={"decision": "rework"},
                owner_actor_id=OWNER,
            )
            assert r["state"] == TaskState.QUEUED
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="task.drive",
                    payload={"task_id": t},
                )
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert (await repo.get_task(t))["state"] == TaskState.SUCCEEDED
            assert [a["step_id"] for a in await repo.list_attempts(t)] == [
                "plan",
                "answer",
                "answer",
                "answer",
            ]
        await close(runner)
    finally:
        await fake.close()


async def test_human_stop_fails_task(db):
    bad = json.dumps({"answer": "", "citations": []})
    fake = ScriptedAppServer([PLAN, bad, bad])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            it = (await repo.list_interactions(session_id=sid, status="pending"))[0]
            token = next(
                e["payload"]["token"]
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "interaction/opened"
            )
            r = await Ledger(repo).respond_interaction(
                str(it["id"]),
                token=token,
                response={"decision": "stop"},
                owner_actor_id=OWNER,
            )
            assert (
                r["state"] == TaskState.FAILED
                and (await repo.get_session(sid))["wheel"] == "user"
            )
        await close(runner)
    finally:
        await fake.close()


async def test_budget_hard_stop_mid_task(db):
    # 每 turn 0.01；预算 0.06：第一步预留 0.05 可开，用 0.01；第二步预留 0.05 > 可用 0.05？可用 = 0.06-0.01 = 0.05 → 可开；改预算 0.055
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake, budget="0.055")
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t)
            assert (
                task["state"] == TaskState.WAITING
                and task["waiting_reason"] == "RESOURCE"
            ), task
            assert task["current_step"] == 1  # 第一步过了
            it = (await repo.list_interactions(session_id=sid, status="pending"))[0]
            assert it["kind"] == "resource"
            token = next(
                e["payload"]["token"]
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "interaction/opened"
            )
            r = await Ledger(repo).respond_interaction(
                str(it["id"]),
                token=token,
                response={"decision": "topup", "amount": "1"},
                owner_actor_id=OWNER,
            )
            assert r["state"] == TaskState.QUEUED
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="task.drive",
                    payload={"task_id": t},
                )
        await drive(runner)
        async with db() as s:
            assert (await WorkbenchRepository(s).get_task(t))[
                "state"
            ] == TaskState.SUCCEEDED
        await close(runner)
    finally:
        await fake.close()


async def test_cancel_between_steps(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        async with db() as s:
            await Ledger(WorkbenchRepository(s)).request_cancel(
                t, owner_actor_id=OWNER
            )  # RECEIVED：直接收敛
        await drive(runner)
        async with db() as s:
            task = await WorkbenchRepository(s).get_task(t)
            assert task["state"] == TaskState.CANCELLED
        assert fake.turn_inputs == []
        await close(runner)
    finally:
        await fake.close()


async def test_unknown_profile_is_rejected_readably(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake, profile="NOT_A_PACK")
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t)
            assert (
                task["state"] == TaskState.REJECTED
                and "不在专家范围" in task["rejection"]["message"]
            )
            assert (await repo.get_session(sid))["wheel"] == "user"
        await close(runner)
    finally:
        await fake.close()


async def test_new_task_binds_the_pack_version_on_shelf(db):
    """2026-10-14 现网：财务分析与预测升到第 2 版，入口不传版本，账房仍记 "1"，受理时找不到专家包就打回。"""
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake, profile="FIN_FORECAST")
        async with db() as s:
            task = await WorkbenchRepository(s).get_task(t)
            assert task["profile_version"] == FIN_FORECAST.version != "1"
            assert (
                find_pack(task["profile_id"], task["profile_version"]) is FIN_FORECAST
            )
        await close(runner)
    finally:
        await fake.close()


async def test_task_bound_to_a_retired_version_says_so(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                task = await repo.get_task(t)
                await repo.cas_task(
                    t, expected_version=int(task["state_version"]), profile_version="0"
                )
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t)
            assert task["state"] == TaskState.REJECTED
            assert task["rejection"]["code"] == "pack_version_gone"
            back = [
                e
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "wheel/return"
            ]
            assert back[-1]["payload"]["final_state"] == TaskState.REJECTED
            assert "旧版" in back[-1]["payload"]["reason_text"]
        await close(runner)
    finally:
        await fake.close()


async def test_positioning_advice_is_blocked(db):
    advice = json.dumps(
        {"answer": "强烈买入，目标价 100 元", "citations": ["x"], "conclusion": ""}
    )
    fake = ScriptedAppServer([PLAN, advice, advice])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t)
            assert (
                task["state"] == TaskState.WAITING and task["waiting_reason"] == "INPUT"
            )
            rejected = [
                e
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "step/rejected"
            ]
            assert any(
                "买卖建议" in f for e in rejected for f in e["payload"]["failures"]
            )
        await close(runner)
    finally:
        await fake.close()


def test_judge_tolerates_fences_and_flags_missing_keys():
    rules = (
        AcceptanceRule(kind="json_object"),
        AcceptanceRule(kind="required_keys", keys=("a",)),
        AcceptanceRule(kind="non_empty", path="a"),
    )
    assert judge('```json\n{"a": "x"}\n```', rules).ok
    assert judge('Sure! Here it is: {"a": "x"} hope it helps', rules).ok
    v = judge('{"b": 1}', rules)
    assert not v.ok and any("required_keys" in f for f in v.failures)
    assert not judge("nope", rules).ok
    assert judge(json.dumps({"plan": ["a"]}), SMOKE.workflow[0].acceptance).ok


def test_writes_files_follows_the_pack():
    from app.application.workbench.ledger import writes_files

    assert writes_files({"profile_id": "SMOKE", "profile_version": SMOKE.version})
    # 只回结构化结果、不写成稿文件的分身（快照里每一步都没有 output_file）
    bare = SMOKE.model_copy(
        update={
            "workflow": tuple(
                st.model_copy(update={"output_file": None}) for st in SMOKE.workflow
            )
        }
    )
    snapshot = {
        "pack_source": "knowlab-test",
        "pack_snapshot": bare.model_dump(mode="json"),
        "pack_digest": "d-no-files",
        "profile_id": "knowbot-test-x",
    }
    assert writes_files(snapshot) is False
    # 认不出专家包（旧版已下架）：按会写算，照旧指向底稿
    assert writes_files({"profile_id": "FIN_FORECAST", "profile_version": "0"}) is True
