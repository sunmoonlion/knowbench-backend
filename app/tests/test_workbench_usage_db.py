"""实时的金额与随时能停（所有者 2026-10-04：页面上实时显示花了多少，加一个随时能点的停止，
不靠预算限额）。假的对面按真的 Codex 的样子报用量；真数据库；经接口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from preview_replay import HANG, Plan, ReplayAppServer
from preview_samples import HENGRUI, fin_review_tools
from sqlalchemy import text
from test_preview_fixtures import fin_review
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_project_routes import machine, project
from test_workbench_routes import A, B
from test_workbench_routes import make_client as make_client  # noqa: F401

from app.bootstrap.workbench import build_runner
from app.domain.workbench.pricing import parse_prices
from app.infrastructure.workbench.publisher import RedisPublisher

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")

TAPES = json.loads(
    (Path(__file__).parent / "preview_tapes" / "conversations.json").read_text()
)["conversations"]


class Desk:
    """一个用户、一台机器、一个沙箱（假的对面）、一个项目，和转着的 runner。"""

    def __init__(self, http, runner, fake, project_id):
        self.http, self.runner, self.fake, self.project = http, runner, fake, project_id

    async def until(self, check, seconds: float = 20.0) -> bool:
        for _ in range(int(seconds / 0.05)):
            await self.runner.run_once()
            if await check():
                return True
            await asyncio.sleep(0.05)
        return False

    async def events(self, session: str) -> list[dict]:
        got = await self.http.get(
            f"/api/workbench/sessions/{session}/events", params={"limit": 1000}
        )
        return got.json()["events"]

    async def task(self, task_id: str) -> dict:
        return (await self.http.get(f"/api/workbench/tasks/{task_id}")).json()["task"]

    async def sheet(self, task_id: str) -> dict:
        got = await self.http.get(f"/api/workbench/tasks/{task_id}/steps")
        return got.json()["task"]

    async def delegate(self, plan: Plan, **body) -> dict:
        self.fake.next_thread(plan)
        made = await self.http.post(
            f"/api/workbench/projects/{self.project}/delegations",
            json={
                "idempotency_key": "usage-test-0001",
                "expert": "FIN_REVIEW",
                "question": HENGRUI.question(),
                **body,
            },
        )
        assert made.status_code == 201, made.text
        return made.json()

    async def state(self, task_id: str, wanted: str) -> bool:
        async def there() -> bool:
            return (await self.task(task_id))["state"] == wanted

        return await self.until(there, 40)


async def desk(make_client, db, *, prices=None):  # noqa: F811
    fake = ReplayAppServer()
    await fake.start()
    runner = build_runner(
        db,
        publisher=RedisPublisher(None, "t"),
        runner_id="usage",
        poll_seconds=0.05,
        prices=prices,
    )
    http = make_client(A)
    env = await machine(http, sandbox=False)
    await http.post(
        "/api/workbench/sandboxes",
        json={
            "app_server_url": f"ws://127.0.0.1:{fake.port}",
            "token_ref": "inline:t",
        },
    )
    made = await project(http, env)
    return Desk(http, runner, fake, made.json()["id"])


async def close(d: Desk) -> None:
    for link in d.runner.links.values():
        if link.client:
            await link.client.close()
    for driving in d.runner.driving.values():
        driving.cancel()
    await d.http.aclose()
    await d.fake.close()


# ---------------- 金额跟着每一次调用跳 ----------------
async def test_each_model_call_is_priced_as_it_arrives(make_client, db):  # noqa: F811
    """一段工作：真的 Codex 录下来的两轮。第一轮调用了四次模型，金额跳四次。"""
    d = await desk(make_client, db)
    try:
        turns = json.loads(json.dumps(TAPES["work"]["turns"]))[:1]
        d.fake.next_thread(Plan(tape=turns))
        made = await d.http.post(
            "/api/workbench/sessions", json={"kind": "work", "project_id": d.project}
        )
        session = made.json()["session_id"]

        async def started() -> bool:
            got = await d.http.get(f"/api/workbench/sessions/{session}")
            return bool(got.json()["session"]["thread_id"])

        assert await d.until(started)
        await d.http.post(
            f"/api/workbench/sessions/{session}/turns",
            json={"text": turns[0]["prompt"]},
        )

        async def over() -> bool:
            return any(e["type"] == "turn/completed" for e in await d.events(session))

        assert await d.until(over)
        events = await d.events(session)
        costs = [
            e["payload"]["cost"]
            for e in events
            if e["type"] == "thread/tokenUsage/updated"
        ]
        assert len(costs) == 4
        assert all(c["priced"] and c["model"] == "kimi-k3" for c in costs)
        assert all(c["currency"] == "CNY" and c["estimated"] for c in costs)
        calls = [float(c["call"]) for c in costs]
        so_far = [float(c["turn"]) for c in costs]
        # 每一次都花了钱；「这一轮到现在」一次比一次多，正好是各次相加
        assert all(call > 0 for call in calls)
        assert so_far == sorted(so_far)
        assert abs(so_far[-1] - sum(calls)) < 1e-6
        started_event = next(e for e in events if e["type"] == "session/thread_started")
        assert started_event["payload"]["model"] == "kimi-k3"

        usage = (await d.http.get(f"/api/workbench/sessions/{session}/usage")).json()
        assert usage["contract_version"] == 2
        assert usage["calls"] == 4 and len(usage["turns"]) == 1
        assert abs(float(usage["cost"]) - sum(calls)) < 1e-6
        assert usage["turns"][0]["cost"] == usage["cost"]
        assert usage["tokens"]["total"] == sum(
            usage["tokens"][k]
            for k in ("input", "cached_input", "cache_write", "output")
        )
        assert usage["price"]["per_million"]["output"] == "100"
        assert usage["price"]["estimated"] is True

        other = make_client(B)
        assert (
            await other.get(f"/api/workbench/sessions/{session}/usage")
        ).status_code == 404
        await other.aclose()
    finally:
        await close(d)


async def test_a_model_without_a_price_shows_tokens_and_no_amount(make_client, db):  # noqa: F811
    d = await desk(
        make_client,
        db,
        prices=parse_prices(
            '{"models": {"another-model": {"input": "1", "cached_input": "1",'
            ' "cache_write": "1", "output": "1"}}}'
        ),
    )
    try:
        made = await d.delegate(
            Plan(expert=fin_review(HENGRUI), tools=fin_review_tools(HENGRUI))
        )
        assert await d.state(made["task_id"], "SUCCEEDED")
        events = await d.events(made["session_id"])
        costs = [
            e["payload"]["cost"]
            for e in events
            if e["type"] == "thread/tokenUsage/updated"
        ]
        assert len(costs) == 7
        assert all(c["priced"] is False and c["call"] is None for c in costs)
        assert all(c["call_tokens"]["total"] > 0 for c in costs)
        sheet = await d.sheet(made["task_id"])
        assert sheet["budget"]["spent"] == "0.00"
        usage = (
            await d.http.get(f"/api/workbench/sessions/{made['session_id']}/usage")
        ).json()
        assert usage["cost"] is None
        assert usage["tokens"]["total"] > 0
    finally:
        await close(d)


# ---------------- 一步花多少：这一步里各次调用相加，不是这条线的累计 ----------------
async def test_a_step_costs_its_own_calls_not_the_running_total_of_the_thread(
    make_client,
    db,  # noqa: F811
):
    d = await desk(make_client, db)
    try:
        made = await d.delegate(
            Plan(
                expert=fin_review(HENGRUI), tools=fin_review_tools(HENGRUI), tokens=5000
            )
        )
        assert await d.state(made["task_id"], "SUCCEEDED")
        shown = (
            await d.http.get(f"/api/workbench/tasks/{made['task_id']}/steps")
        ).json()
        # 七步，每步一次调用，每次 5000 个 token：每步 0.05，一共 0.35。
        # 拿累计当一步的话会是 0.05、0.10、0.15……一共 1.40
        assert [s["spent"] for s in shown["steps"]] == ["0.05"] * 7
        assert shown["task"]["budget"] == {
            "currency": "CNY",
            "limit": None,
            "used": "0.35",
            "reserved": "0.00",
            "left": None,
            "running": "0.00",
            "spent": "0.35",
            "estimated": True,
        }
    finally:
        await close(d)


# ---------------- 没有上限：花多少记多少，不自己停 ----------------
async def test_without_a_limit_the_expert_is_never_stopped_for_money(make_client, db):  # noqa: F811
    d = await desk(make_client, db)
    try:
        made = await d.delegate(
            Plan(
                expert=fin_review(HENGRUI),
                tools=fin_review_tools(HENGRUI),
                tokens=300000,
            )
        )
        assert await d.state(made["task_id"], "SUCCEEDED")
        sheet = await d.sheet(made["task_id"])
        assert sheet["budget"]["limit"] is None and sheet["budget"]["left"] is None
        assert sheet["budget"]["spent"] == "21.00"  # 七步，每步 3.00
        events = await d.events(made["session_id"])
        assert not [e for e in events if e["type"] == "interaction/opened"]
    finally:
        await close(d)


async def test_a_limit_still_works_when_one_is_given(make_client, db):  # noqa: F811
    d = await desk(make_client, db)
    try:
        made = await d.delegate(
            Plan(
                expert=fin_review(HENGRUI),
                tools=fin_review_tools(HENGRUI),
                tokens=30000,
            ),
            budget_limit="1",
        )
        assert await d.state(made["task_id"], "WAITING")
        task = await d.task(made["task_id"])
        assert task["waiting_reason"] == "RESOURCE"
        sheet = await d.sheet(made["task_id"])
        assert sheet["budget"]["limit"] == "1.00"
    finally:
        await close(d)


# ---------------- 随时能停 ----------------
async def test_stopping_the_expert_in_the_middle_of_a_step(make_client, db):  # noqa: F811
    d = await desk(make_client, db)
    try:
        made = await d.delegate(
            Plan(
                expert=[*fin_review(HENGRUI)[:2], HANG],
                tools=fin_review_tools(HENGRUI),
                tokens=5000,
            )
        )
        task_id, session = made["task_id"], made["session_id"]

        async def third_step_running() -> bool:
            sheet = await d.sheet(task_id)
            # 前两步结了账，第三步的那一次调用也报上来了
            budget = sheet["budget"]
            return (budget["used"], budget["running"]) == ("0.10", "0.05")

        assert await d.until(third_step_running, 40)
        # 现在在干什么、为什么、没有卡住：第 3 步，工具用完了，在想；runner 握着沙箱
        shown = (await d.http.get(f"/api/workbench/tasks/{task_id}/steps")).json()
        now = shown["now"]
        assert (now["step"]["index"], now["step"]["title"]) == (3, "勾稽")
        assert now["step"]["why"].startswith("三张表对不上")
        assert now["doing"]["code"] == "thinking" and now["held"] is True
        assert now["since"] <= now["last_event_at"]
        sheet = await d.sheet(task_id)
        # 做完的两步记了 0.10；正在做的这一步已经调用过一次模型，又是 0.05：页面上是 0.15
        assert (sheet["budget"]["used"], sheet["budget"]["running"]) == ("0.10", "0.05")
        assert sheet["budget"]["spent"] == "0.15"
        assert d.fake.interrupted == []

        stop = await d.http.post(f"/api/workbench/tasks/{task_id}/cancel")
        assert stop.status_code == 202 and stop.json()["cancel_requested"] is True
        assert await d.state(task_id, "CANCELLED")

        # 正在跑的那一轮被停下了，不是等它自己跑完
        assert len(d.fake.interrupted) == 1 and d.fake.interrupted[0]
        events = await d.events(session)
        types = [e["type"] for e in events]
        assert "turn/stop_requested" in types and "wheel/return" in types
        stopped = next(e for e in events if e["type"] == "turn/stop_requested")
        assert stopped["payload"] == {
            "turn_id": d.fake.interrupted[0],
            "by": "user",
            "running": True,
        }
        # 被停下的那一步，已经花的照记
        after = (await d.http.get(f"/api/workbench/tasks/{task_id}/steps")).json()
        assert after["task"]["state_word"] == "已取消"
        assert after["task"]["reason_text"] == "你取消了这个委托"
        assert after["task"]["budget"]["spent"] == "0.15"
        assert after["task"]["budget"]["running"] == "0.00"
        assert after["now"] is None  # 结束了，没有「现在」
        assert [s["status"] for s in after["steps"]][:4] == [
            "accepted",
            "accepted",
            "cancelled",
            "not_reached",
        ]
        third = after["steps"][2]
        assert third["spent"] == "0.05"
        assert third["attempts"][0]["outcome"] == "cancelled"
        # 方向盘交回了：用户可以接着自己做
        talk = (await d.http.get(f"/api/workbench/sessions/{session}")).json()[
            "session"
        ]
        assert talk["wheel"] == "user" and talk["active_task_id"] is None
        async with db() as s:
            states = (
                (
                    await s.execute(
                        text(
                            "select status from workbench_attempts order by created_at"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert states == ["COMPLETED", "COMPLETED", "CANCELLED"]
    finally:
        await close(d)


async def test_stopping_my_own_turn_needs_no_turn_id(make_client, db):  # noqa: F811
    """聊天、工作里的停止：页面不用知道是哪一轮，停的是这条线正在跑的那一轮。"""
    d = await desk(make_client, db)
    try:
        # 磁带里「工作」的第二轮停在等批准：这一轮一直开着
        turns = json.loads(json.dumps(TAPES["work"]["turns"]))[1:]
        d.fake.next_thread(Plan(tape=turns))
        session = (
            await d.http.post(
                "/api/workbench/sessions",
                json={"kind": "work", "project_id": d.project},
            )
        ).json()["session_id"]

        async def started() -> bool:
            got = await d.http.get(f"/api/workbench/sessions/{session}")
            return bool(got.json()["session"]["thread_id"])

        assert await d.until(started)
        await d.http.post(
            f"/api/workbench/sessions/{session}/turns",
            json={"text": turns[0]["prompt"]},
        )

        async def asked() -> bool:
            return any(
                e["type"] == "interaction/opened" for e in await d.events(session)
            )

        assert await d.until(asked)
        stop = await d.http.post(
            f"/api/workbench/sessions/{session}/interrupt", json={}
        )
        assert stop.status_code == 202

        async def stopped() -> bool:
            return any(e["type"] == "turn/completed" for e in await d.events(session))

        assert await d.until(stopped)
        events = await d.events(session)
        done = next(e for e in events if e["type"] == "turn/completed")
        assert done["payload"]["turn"]["status"] == "interrupted"
        noted = next(e for e in events if e["type"] == "turn/stop_requested")
        assert noted["payload"]["running"] is True
        assert noted["payload"]["turn_id"] == d.fake.interrupted[0]

        # 没有在跑的时候再点：什么都不停，如实记一笔
        again = await d.http.post(
            f"/api/workbench/sessions/{session}/interrupt", json={}
        )
        assert again.status_code == 202

        async def twice() -> bool:
            now = await d.events(session)
            return len([e for e in now if e["type"] == "turn/stop_requested"]) == 2

        assert await d.until(twice)
        last = [
            e for e in await d.events(session) if e["type"] == "turn/stop_requested"
        ]
        assert last[-1]["payload"] == {"turn_id": None, "by": "user", "running": False}
        assert len(d.fake.interrupted) == 1
    finally:
        await close(d)


# ---------------- 重新接上一条线：对面把上一次的用量再报一遍 ----------------
async def test_usage_reported_again_after_a_reconnect_is_not_counted_twice(
    make_client,
    db,  # noqa: F811
):
    """真的 Codex 在重新装载一条线时，会把最近一次的用量原样再报一遍（它的源码
    `token_usage_replay.rs`）。照「每来一条就加一次」算，会多算一次调用的钱。"""
    d = await desk(make_client, db)
    try:
        turns = json.loads(json.dumps(TAPES["work"]["turns"]))
        d.fake.next_thread(Plan(tape=turns))
        session = (
            await d.http.post(
                "/api/workbench/sessions",
                json={"kind": "work", "project_id": d.project},
            )
        ).json()["session_id"]

        async def started() -> bool:
            got = await d.http.get(f"/api/workbench/sessions/{session}")
            return bool(got.json()["session"]["thread_id"])

        assert await d.until(started)
        await d.http.post(
            f"/api/workbench/sessions/{session}/turns",
            json={"text": turns[0]["prompt"]},
        )

        async def one_turn_over() -> bool:
            return any(e["type"] == "turn/completed" for e in await d.events(session))

        assert await d.until(one_turn_over)
        before = (await d.http.get(f"/api/workbench/sessions/{session}/usage")).json()
        assert before["calls"] == 4

        # 连接断过、进程重启过：内存里什么都不记得了，要从账里找回来
        for link in d.runner.links.values():
            link.live_threads.clear()
            link.totals.clear()
            link.models.clear()
        await d.http.post(
            f"/api/workbench/sessions/{session}/turns",
            json={"text": turns[1]["prompt"]},
        )

        async def asked() -> bool:
            return any(
                e["type"] == "interaction/opened" for e in await d.events(session)
            )

        assert await d.until(asked)
        assert d.fake.replayed == 1  # 对面确实又报了一遍
        events = await d.events(session)
        reports = [e for e in events if e["type"] == "thread/tokenUsage/updated"]
        totals = [json.dumps(e["payload"]["tokenUsage"]["total"]) for e in reports]
        assert len(totals) == len(set(totals))  # 账里没有两条一样的
        after = (await d.http.get(f"/api/workbench/sessions/{session}/usage")).json()
        second = [
            e
            for e in reports
            if e["payload"]["turnId"] != reports[0]["payload"]["turnId"]
        ]
        assert after["calls"] == 4 + len(second)
        assert after["turns"][0] == before["turns"][0]  # 第一轮的钱没有变多
        assert all(e["payload"]["cost"]["model"] == "kimi-k3" for e in second)
    finally:
        await close(d)
