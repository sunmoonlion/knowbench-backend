"""整条链的驱动：工作台（账房 + runner，进程内）→ 沙箱 app-server（容器，ws + 令牌）→ 会合点 → 本地代理 → 本机文件。

不走 HTTP 与登录（路由另有 tests/test_workbench_routes.py），直接用应用层服务，验的是工作台与沙箱、代理之间的真实往来。
env：AGENT_TEST_DATABASE_URL（*_tests 库，脚本自建 schema 跑迁移）、APP_SERVER_URL、APP_SERVER_TOKEN、ROOT（本机白名单目录）、ENV_KEY（默认 user-pc）
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import os.path
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.application.workbench.ledger import Ledger  # noqa: E402
from app.application.workbench.runner import Runner  # noqa: E402
from app.application.workbench.session_service import SessionService  # noqa: E402
from app.bootstrap.workbench import build_runner  # noqa: E402
from app.domain.workbench.errors import WheelHeldByOther  # noqa: E402
from app.domain.workbench.models import HandoverRequest  # noqa: E402
from app.domain.workbench.step_view import amount as step_amount  # noqa: E402
from app.infrastructure.storage.postgres import make_session_factory
from app.infrastructure.workbench.publisher import RedisPublisher  # noqa: E402
from app.infrastructure.workbench.repository import WorkbenchRepository  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parents[2]
USAGE_EVENT = "thread/tokenUsage/updated"
OWNER = str(uuid.uuid4())
fails = 0


def verdict(name: str, ok: bool, extra: str = "") -> None:
    global fails
    print(f"  VERDICT {name:<58} {'pass' if ok else 'fail'} {extra}", flush=True)
    if not ok:
        fails += 1


def migrate(connection):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(connection)):
        for path in sorted((ROOT_DIR / "alembic/versions").glob("20*.py")):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade()


async def events_of(factory, sid: str, after: int = 0) -> list[dict]:
    """一段对话里游标在 after 之后的全部事件。一次最多取 500 条，所以翻页取完。"""
    out: list[dict] = []
    async with factory() as s:
        repo = WorkbenchRepository(s)
        while True:
            page = await repo.list_events(session_id=sid, after_cursor=after)
            if not page:
                return out
            out.extend(page)
            after = int(page[-1]["cursor"])


async def wait_event(
    factory,
    sid: str,
    event_type: str,
    timeout: float = 240,  # noqa: ASYNC109
    *,
    runner: Runner | None = None,
) -> dict | None:
    """等某种事件出现。等的时候让 runner 接着转：沙箱的租约只有十秒，不续就会被当成丢了。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in await events_of(factory, sid):
            if e["type"] == event_type:
                return e
        if runner is not None:
            await runner.run_once()
        await asyncio.sleep(0.5)
    return None


async def pump(runner: Runner, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if await runner.run_once() == 0:
            await asyncio.sleep(0.3)


async def main() -> int:
    url = os.environ["AGENT_TEST_DATABASE_URL"]
    app_url = os.environ.get("APP_SERVER_URL", "ws://127.0.0.1:47800")
    token = os.environ["APP_SERVER_TOKEN"]
    root = os.environ["ROOT"]
    env_key = os.environ.get("ENV_KEY", "user-pc")
    schema = "wb_chain_" + uuid.uuid4().hex[:8]
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
        runner_id="chain-driver",
        environment_key=env_key,
        poll_seconds=0.2,
    )
    ts = int(time.time())
    try:
        print("--- 1. 登记环境、沙箱，建会话")
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
                sb = await repo.register_sandbox(
                    owner_actor_id=OWNER,
                    app_server_url=app_url,
                    token_ref=f"inline:{token}",
                    codex_version="0.155.1",
                )
            sid = (
                await SessionService(repo).create(
                    owner_actor_id=OWNER,
                    environment_id=env,
                    sandbox_id=sb,
                    project_root=root,
                    thread_settings={
                        "approvalPolicy": "on-request",
                        "sandbox": "workspace-write",
                    },
                )
            )["session_id"]
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="session.start_thread",
                    payload={},
                )
        await pump(runner, 2)
        async with factory() as s:
            sess = await WorkbenchRepository(s).get_session(sid)
        verdict(
            "thread/start 经 ws+令牌成功，thread_id 落库",
            bool(sess["thread_id"]),
            str(sess["thread_id"]),
        )

        print("--- 2. 用户自驾：在白名单目录写文件（命令在本机执行）")
        fname = f"PROBE_WB_{ts}.txt"
        async with factory() as s:
            repo = WorkbenchRepository(s)
            await SessionService(repo).record_user_turn_requested(
                sid, owner_actor_id=OWNER, text="t1", request_id="req-1"
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": f"Run exactly `echo via-workbench > {fname} && cat {fname}` and report verbatim.",
                        "request_id": "req-1",
                        "by": "user",
                    },
                )
        await pump(runner, 1)
        done = await wait_event(factory, sid, "turn/completed", runner=runner)
        verdict("turn/completed 事件到达", done is not None)
        target = Path(root) / fname
        verdict(
            "文件出现在本机白名单目录",
            os.path.exists(target)  # noqa: ASYNC240
            and Path(target).read_text().strip() == "via-workbench",  # noqa: ASYNC240,
        )
        types = [e["type"] for e in await events_of(factory, sid)]
        verdict(
            "事件里有 item/completed 且没有 delta",
            "item/completed" in types
            and not any(t.lower().endswith("delta") for t in types),
        )

        print(
            "--- 3. 用户自驾：写白名单外 → Codex 请求审批 → 工作台开 Interaction → 我们批准 → 本地上限仍然拒绝"
        )
        outside = f"/home/zym/probe-wb-outside-{ts}.txt"
        async with factory() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": f"Run exactly `touch {outside} && echo touched`. If it needs approval, request it. Do not retry; report the final result verbatim.",
                        "request_id": "req-2",
                        "by": "user",
                    },
                )
        await pump(runner, 1)
        opened = await wait_event(
            factory, sid, "interaction/opened", timeout=180, runner=runner
        )
        verdict(
            "审批请求变成 tool_approval Interaction",
            opened is not None and opened["payload"].get("kind") == "tool_approval",
        )
        if opened is None:
            evs = await events_of(factory, sid)
            print("  [debug] 第三段的事件：")
            for e in evs:
                if e["cursor"] > 8:
                    p = e["payload"]
                    brief = (
                        p.get("item", {}).get("text")
                        if isinstance(p.get("item"), dict)
                        else None
                    )
                    print(
                        "   ",
                        e["cursor"],
                        e["type"],
                        (brief or str(p))[:300].replace("\n", " | "),
                    )
        if opened:
            async with factory() as s:
                repo = WorkbenchRepository(s)
                it = (await repo.list_interactions(session_id=sid, status="pending"))[0]
                await Ledger(repo).respond_interaction(
                    str(it["id"]),
                    token=opened["payload"]["token"],
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
            await pump(runner, 1)
            # 第二个 turn/completed；期间模型可能再次请求审批（被本地上限拒后重试），后续一律拒绝
            deadline = time.time() + 300
            second = None
            answered = {str(it["id"])}
            while time.time() < deadline and second is None:
                evs = await events_of(factory, sid)
                async with factory() as s:
                    repo = WorkbenchRepository(s)
                    done_evs = [e for e in evs if e["type"] == "turn/completed"]
                    if len(done_evs) >= 2:
                        second = done_evs[1]
                        break
                    for pend in await repo.list_interactions(
                        session_id=sid, status="pending"
                    ):
                        if (
                            str(pend["id"]) in answered
                            or pend["kind"] != "tool_approval"
                        ):
                            continue
                        tok = next(
                            e["payload"]["token"]
                            for e in evs
                            if e["type"] == "interaction/opened"
                            and e["payload"]["interaction_id"] == str(pend["id"])
                        )
                        await Ledger(repo).respond_interaction(
                            str(pend["id"]),
                            token=tok,
                            response={"decision": "decline"},
                            owner_actor_id=OWNER,
                        )
                        async with repo.transaction():
                            await repo.enqueue_command(
                                session_id=sid,
                                sandbox_id=sb,
                                kind="approval.respond",
                                payload={
                                    "request_id": pend["prompt"]["subject"][
                                        "request_id"
                                    ],
                                    "decision": "decline",
                                },
                            )
                        answered.add(str(pend["id"]))
                await pump(runner, 0.5)
            verdict("批准后的 turn 结束", second is not None)
            verdict("白名单外文件没有出现（本地上限兜底）", not os.path.exists(outside))  # noqa: ASYNC240
            async with factory() as s:
                r = await s.execute(
                    text(
                        "select decision, source from workbench_approval_log order by created_at"
                    )
                )
                rows = [tuple(x) for x in r.all()]
            verdict(
                "审批留痕 decision=accept source=user",
                rows == [("accept", "user")],
                str(rows),
            )

        print(
            "--- 4. 交出方向盘，顾问按 SMOKE 专家包驾驶同一个 thread（两步、验收、预算、交回）"
        )
        async with factory() as s:
            repo = WorkbenchRepository(s)
            led = Ledger(repo)
            t_smoke = (
                await led.handover(
                    HandoverRequest(
                        idempotency_key=f"smoke-{ts}",
                        session_id=sid,
                        profile_id="SMOKE",
                        original_input={
                            "text": "What files are in this project directory and what do they contain? Cite paths."
                        },
                    ),  # 不给预算上限：花多少记多少，不自己停
                    owner_actor_id=OWNER,
                )
            )["task_id"]
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="task.drive",
                    payload={"task_id": t_smoke},
                )
        before_smoke = (await events_of(factory, sid))[-1]["cursor"]
        await runner.run_once()
        await runner.wait_drivers(timeout=600)
        async with factory() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t_smoke)
            arts = await repo.list_artifacts(t_smoke)
            attempts = await repo.list_attempts(t_smoke)
            sess = await repo.get_session(sid)
            print(
                "  task state:",
                task["state"],
                "step:",
                task["current_step"],
                "budget:",
                task["budget"],
            )
            print(
                "  attempts:",
                [(a["step_id"], a["status"], a["failure_code"]) for a in attempts],
            )
            print("  artifacts:", [(a["name"], a["version"]) for a in arts])
            ans = await repo.get_artifact(task_id=t_smoke, name="answer")
            if ans:
                print("  answer:", str(ans["content"])[:300])
        verdict(
            "顾问驾驶到 SUCCEEDED（真沙箱、Kimi、本机执行）",
            task["state"] == "SUCCEEDED",
            task["state"],
        )
        verdict(
            "产物 plan/answer/handback 各至少一版",
            {a["name"] for a in arts} >= {"plan", "answer", "handback"},
        )
        verdict(
            "预算有扣减（token 记账）",
            Decimal(task["budget"]["used"]) > 0,
            task["budget"]["used"],
        )
        verdict("终态后方向盘交回用户", sess["wheel"] == "user")
        verdict(
            "没给上限：账上没有上限，专家做到底",
            task["budget"].get("limit") is None and task["state"] == "SUCCEEDED",
            str(task["budget"].get("limit")),
        )
        usage_events = [
            e for e in await events_of(factory, sid) if e["type"] == USAGE_EVENT
        ]
        costs = [e["payload"].get("cost") or {} for e in usage_events]
        verdict(
            "每条用量事件都带金额（kimi-k3，人民币）",
            bool(costs)
            and all(
                c.get("priced") and c.get("model") == "kimi-k3" and c.get("call")
                for c in costs
            ),
            f"{len(costs)} 条",
        )
        smoke_calls = sum(
            Decimal(e["payload"]["cost"]["call"])
            for e in usage_events
            if e["cursor"] > before_smoke
            and (e["payload"].get("cost") or {}).get("call")
        )
        consumed = sum(
            (step_amount(a.get("budget_consumed")) for a in attempts), Decimal("0")
        )
        print(
            "  各步记的：",
            [str(step_amount(a.get("budget_consumed"))) for a in attempts],
        )
        verdict(
            "专家各步记的花费 = 这几步里每次调用相加",
            abs(consumed - smoke_calls) < Decimal("0.0001")
            and abs(Decimal(task["budget"]["used"]) - consumed) < Decimal("0.0001"),
            f"各步 {consumed}，各次调用 {smoke_calls}，账上 {task['budget']['used']}",
        )

        print("--- 5. 再次交出方向盘：用户 turn 被拒；取消后交回")
        async with factory() as s:
            repo = WorkbenchRepository(s)
            led = Ledger(repo)
            t = (
                await led.handover(
                    HandoverRequest(
                        idempotency_key=f"chain2-{ts}",
                        session_id=sid,
                        profile_id="SMOKE",
                        original_input={"text": "核对"},
                        budget_limit=Decimal("5"),
                    ),
                    owner_actor_id=OWNER,
                )
            )["task_id"]
            refused = False
            try:
                await SessionService(repo).assert_user_may_drive(
                    sid, owner_actor_id=OWNER
                )
            except WheelHeldByOther:
                refused = True
            verdict("advisor 持方向盘时用户 turn 被拒", refused)
            r = await led.request_cancel(t, owner_actor_id=OWNER)
            verdict(
                "取消后 Task CANCELLED、方向盘交回",
                r["state"] == "CANCELLED"
                and (await repo.get_session(sid))["wheel"] == "user",
            )
        print("--- 6. 随时能停：自己的一轮")
        async with factory() as s:
            repo = WorkbenchRepository(s)
            await SessionService(repo).record_user_turn_requested(
                sid, owner_actor_id=OWNER, text="long", request_id="req-long"
            )
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.start",
                    payload={
                        "text": "Without using any tools, write a detailed 3000-word essay"
                        " on the history of double-entry bookkeeping.",
                        "request_id": "req-long",
                        "by": "user",
                    },
                )
        mark = (await events_of(factory, sid))[-1]["cursor"]
        started = None
        deadline = time.time() + 60
        while time.time() < deadline and started is None:
            await runner.run_once()
            started = next(
                (
                    e
                    for e in await events_of(factory, sid, after=mark)
                    if e["type"] == "turn/started"
                ),
                None,
            )
            await asyncio.sleep(0.3)
        await pump(runner, 4)  # 让它写一会儿
        pressed = time.time()
        async with factory() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.interrupt",
                    payload={"by": "user"},  # 页面不给轮次的编号
                )
        ended = None
        deadline = time.time() + 60
        while time.time() < deadline and ended is None:
            await runner.run_once()
            ended = next(
                (
                    e
                    for e in await events_of(factory, sid, after=mark)
                    if e["type"] == "turn/completed"
                ),
                None,
            )
            await asyncio.sleep(0.3)
        took = time.time() - pressed
        after = await events_of(factory, sid, after=mark)
        noted = next((e for e in after if e["type"] == "turn/stop_requested"), None)
        status = ((ended or {}).get("payload") or {}).get("turn", {}).get("status")
        verdict(
            "不给轮次编号也停得下：这一轮以 interrupted 结束",
            started is not None and status == "interrupted",
            f"status={status}，按下到停下 {took:.1f} 秒",
        )
        verdict(
            "停止被记了一笔，记下停的是哪一轮",
            noted is not None
            and noted["payload"].get("running") is True
            and bool(noted["payload"].get("turn_id")),
        )
        cut = [e for e in after if e["type"] == USAGE_EVENT]
        print(
            "  被停下的这一轮报了几次用量：",
            len(cut),
            [e["payload"]["cost"].get("call") for e in cut],
        )
        every = [e for e in await events_of(factory, sid) if e["type"] == USAGE_EVENT]
        seen = [str(e["payload"]["tokenUsage"].get("total")) for e in every]
        verdict(
            "重新接线时对面重报的用量没有再记一遍",
            len(seen) == len(set(seen)),
            f"{len(seen)} 条，{len(set(seen))} 种累计",
        )

        print("--- 7. 随时能停：专家做到一半")
        async with factory() as s:
            repo = WorkbenchRepository(s)
            led = Ledger(repo)
            t_stop = (
                await led.handover(
                    HandoverRequest(
                        idempotency_key=f"stop-{ts}",
                        session_id=sid,
                        profile_id="SMOKE",
                        original_input={
                            "text": "List every file in this project directory and"
                            " describe each one in detail. Cite paths."
                        },
                    ),
                    owner_actor_id=OWNER,
                )
            )["task_id"]
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="task.drive",
                    payload={"task_id": t_stop},
                )
        mark = (await events_of(factory, sid))[-1]["cursor"]
        running = None
        deadline = time.time() + 90
        while time.time() < deadline and running is None:
            await runner.run_once()
            running = next(
                (
                    e
                    for e in await events_of(factory, sid, after=mark)
                    if e["type"] == "turn/started"
                ),
                None,
            )
            await asyncio.sleep(0.3)
        await pump(runner, 3)
        pressed = time.time()
        async with factory() as s:
            repo = WorkbenchRepository(s)
            asked = await Ledger(repo).request_cancel(t_stop, owner_actor_id=OWNER)
            # 和接口做的一样：记下「要停」，再让 runner 把正在跑的这一轮停下
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="turn.interrupt",
                    payload={"by": "user", "task_id": t_stop},
                )
        state = None
        deadline = time.time() + 90
        while time.time() < deadline and state != "CANCELLED":
            await runner.run_once()
            async with factory() as s:
                state = (await WorkbenchRepository(s).get_task(t_stop))["state"]
            await asyncio.sleep(0.3)
        took = time.time() - pressed
        await runner.wait_drivers(timeout=30)
        async with factory() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t_stop)
            attempts = await repo.list_attempts(t_stop)
            sess = await repo.get_session(sid)
        print(
            "  attempts:",
            [
                (a["step_id"], a["status"], str(step_amount(a.get("budget_consumed"))))
                for a in attempts
            ],
            "budget:",
            task["budget"],
        )
        verdict(
            "专家正在做的那一步被停下（不是等它做完）",
            running is not None
            and asked.get("cancel_requested") is True
            and task["state"] == "CANCELLED"
            and attempts[-1]["status"] == "CANCELLED",
            f"{task['state']}，按下到停下 {took:.1f} 秒",
        )
        verdict("停下后方向盘交回用户", sess["wheel"] == "user")
        try:
            os.unlink(target)  # noqa: ASYNC240
        except FileNotFoundError:
            pass
    finally:
        for link in runner.links.values():
            if link.client:
                await link.client.close()
        await engine.dispose()
        with sync.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        sync.dispose()
    print("结论：" + ("pass" if fails == 0 else f"fail ({fails})"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
