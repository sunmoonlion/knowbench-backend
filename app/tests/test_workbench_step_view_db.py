"""一个委托的步骤视图（PRD/apps/investment-expert.md 第六节；AT-INV-19 至 24、28）。

假 app-server 按脚本回复，真数据库。验的是后端算出来的视图：页面照着画，不自己推状态。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sample_replies import (
    ALL,
    CROSSCHECK,
    FACTS,
    METRICS,
    NOTE,
    PROFILE,
    RECONCILE,
    SCOPE,
    say,
)
from test_workbench_advisor_db import ScriptedAppServer, close, drive, seed
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.ledger import Ledger
from app.domain.workbench.errors import NotFound
from app.infrastructure.workbench.repository import WorkbenchRepository

STRANGER = str(uuid.uuid4())


class ToolingAppServer(ScriptedAppServer):
    """和脚本服务一样，另外在每一轮里先报几次工具调用：tools[i] 是第 i 轮的。"""

    def __init__(self, replies, tools: dict[int, list[dict]] | None = None):
        super().__init__(replies)
        self.tools = tools or {}

    async def handle(self, ws, msg):
        if msg["method"] != "turn/start":
            return await super().handle(ws, msg)
        p, rid = msg["params"], msg["id"]
        send = lambda o: ws.send(json.dumps(o))  # noqa: E731
        tid, turn_id = p["threadId"], f"turn-{uuid.uuid4().hex[:6]}"
        nth = len(self.turn_inputs)
        self.turn_inputs.append(p["input"][0]["text"])
        await send({"id": rid, "result": {"turn": {"id": turn_id, "threadId": tid}}})
        reply = self.replies.pop(0) if self.replies else "echo"
        await asyncio.sleep(0.01)
        for n, item in enumerate(self.tools.get(nth, [])):
            await send(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": tid,
                        "turnId": turn_id,
                        "item": {"id": f"call-{nth}-{n}", **item},
                    },
                }
            )
        for method, params in (
            (
                "item/completed",
                {"item": {"type": "agentMessage", "id": "m", "text": reply}},
            ),
            (
                "thread/tokenUsage/updated",
                {"tokenUsage": self.meter.report(tid, 1000)},
            ),
        ):
            await send(
                {
                    "method": method,
                    "params": {"threadId": tid, "turnId": turn_id, **params},
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


def query(sql: str, *, ok: bool = True) -> dict:
    return {
        "type": "mcpToolCall",
        "server": "knowbase",
        "tool": "run_sql",
        "status": "completed" if ok else "failed",
        "arguments": {"dataset": "sh600276-financials-5868ab1a", "sql": sql},
        "result": {"content": [{"type": "text", "text": "ROWS-THAT-MUST-NOT-LEAK"}]},
        "error": None if ok else {"message": "boom"},
    }


async def run(db, replies, tools=None):
    fake = ToolingAppServer(
        [say(r) if not isinstance(r, str) else r for r in replies], tools
    )
    await fake.start()
    runner, sid, sb, task_id = await seed(db, fake, profile="FIN_REVIEW")
    await drive(runner)
    return fake, runner, sid, sb, task_id


async def view(db, task_id, number=None, owner=OWNER):
    async with db() as s:
        desk = ExpertDesk(WorkbenchRepository(s))
        if number is None:
            return await desk.steps(task_id, owner_actor_id=owner)
        return (await desk.step(task_id, number, owner_actor_id=owner))["step"]


def rail(shown):
    return [
        (s["index"], s["status"], s["times"], s["rejected"]) for s in shown["steps"]
    ]


async def test_seven_steps_done(db):
    """AT-INV-20"""
    fake, runner, *_, task_id = await run(
        db,
        ALL,
        tools={
            2: [
                query("select * from reconciliation_rules"),
                query("select 1 from balance_sheet"),
                {
                    "type": "commandExecution",
                    "command": "ls",
                    "status": "completed",
                    "exitCode": 0,
                },
            ]
        },
    )
    try:
        shown = await view(db, task_id)
        assert rail(shown) == [(n, "accepted", 1, 0) for n in range(1, 8)]
        assert shown["position"] == {"step": 7, "of": 7, "title": "成稿", "left": 0}
        sheet = shown["task"]
        assert sheet["state"] == "SUCCEEDED" and sheet["state_word"] == "已完成"
        assert sheet["expert"]["name"] == "财报体检"
        assert sheet["question"]
        assert sheet["budget"]["used"] == "0.07"
        assert sheet["data"]["dataset"] == PROFILE["dataset"]
        assert sheet["data"]["data_version"] == NOTE["data_version"]
        assert sheet["data"]["versions_seen"] == [NOTE["data_version"]]
        assert sheet["ended_at"] is not None and sheet["seconds"] >= 0
        # 步骤轨不带交回物与过程：那是点开一步才取的
        assert all("returned" not in a for s in shown["steps"] for a in s["attempts"])
        assert shown["steps"][2]["attempts"][0]["tools"] == {"run_sql": 2, "命令": 1}
        assert shown["steps"][2]["spent"] == "0.01"

        step = await view(db, task_id, 3)
        assert step["title"] == "勾稽" and step["summary"]
        assert step["uses"] == [
            {"artifact": "scope", "step": 1, "title": "定范围"},
            {"artifact": "profile", "step": 2, "title": "数据摸底"},
        ]
        (attempt,) = step["attempts"]
        assert attempt["outcome"] == "accepted"
        labels = attempt["returned"].pop("labels")
        assert attempt["returned"] == {
            "artifact": "reconcile",
            "version": 1,
            "content": RECONCILE,
        }
        # 字段各叫什么：页面上不让用户看英文字段名
        assert labels["checks"] == "勾稽结果" and labels["rule"] == "规则"
        assert [(c["label"], c["pass"], c["message"]) for c in attempt["checks"]] == [
            ("交回的格式对", True, ""),
            ("有勾稽结果、跨期连续性、未解释的断点", True, ""),
            ("做了勾稽", True, ""),
            ("每条勾稽规则都平", True, ""),
            ("跨期的断点都有解释", True, ""),
        ]
        assert [
            (p["kind"], p["name"], p["brief"], p["ok"]) for p in attempt["process"]
        ] == [
            ("tool", "run_sql", "select * from reconciliation_rules", True),
            ("tool", "run_sql", "select 1 from balance_sheet", True),
            ("command", "命令", "ls", True),
        ]
        assert attempt["process"][0]["dataset"] == "sh600276-financials-5868ab1a"
        # 过程只给摘要，查询带回来的内容不在里面
        assert "ROWS-THAT-MUST-NOT-LEAK" not in json.dumps(step, default=str)
    finally:
        await close(runner)
        await fake.close()


async def test_a_step_that_was_redone_shows_both_tries(db):
    """AT-INV-21"""
    empty = METRICS | {"table": []}
    fake, runner, *_, task_id = await run(
        db, [*ALL[:4], empty, METRICS, CROSSCHECK, NOTE]
    )
    try:
        shown = await view(db, task_id)
        assert shown["task"]["state"] == "SUCCEEDED"
        assert rail(shown)[4] == (5, "accepted", 2, 1)
        first, second = (await view(db, task_id, 5))["attempts"]
        assert (first["n"], first["outcome"]) == (1, "rejected")
        assert first["returned"]["content"] == empty  # 第一次交回了什么，还看得到
        assert [(c["label"], c["pass"], c["message"]) for c in first["checks"]] == [
            ("交回的格式对", True, ""),
            ("有指标表和数据版本", True, ""),
            ("指标表不是空的", False, "指标表为空"),
        ]
        assert (second["n"], second["outcome"]) == (2, "accepted")
        assert second["returned"]["content"] == METRICS
        assert all(c["pass"] for c in second["checks"])
    finally:
        await close(runner)
        await fake.close()


async def test_an_answer_that_is_not_in_the_agreed_form(db):
    fake, runner, *_, task_id = await run(db, ["我觉得这家公司不错", SCOPE, *ALL[1:]])
    try:
        first, second = (await view(db, task_id, 1))["attempts"]
        assert first["outcome"] == "rejected"
        assert first["returned"]["content"] is None
        assert first["returned"]["raw"] == "我觉得这家公司不错"
        assert [(c["label"], c["pass"]) for c in first["checks"]] == [
            ("交回的格式对", False),
            ("有证券代码、期间、报告类型", None),  # 格式不对，后面的没有验
            ("认得出是哪家公司", None),
            ("期间不是空的", None),
        ]
        assert second["outcome"] == "accepted"
    finally:
        await close(runner)
        await fake.close()


async def test_going_back_to_an_earlier_step_can_be_seen(db):
    """AT-INV-22：取数两次不过，退回数据摸底。脚本到此为止，专家停在重做摸底。"""
    empty = FACTS | {"table": []}
    fake, runner, *_, task_id = await run(
        db, [SCOPE, PROFILE, RECONCILE, empty, empty, "停"]
    )
    try:
        shown = await view(db, task_id)
        statuses = {s["title"]: s["status"] for s in shown["steps"]}
        assert statuses["定范围"] == "accepted"
        assert statuses["取数"] == "went_back"
        assert shown["steps"][3]["after_rejection"]["back_to"] == 2
        assert statuses["勾稽"] == "redo"  # 做过、通过了，但退回之后要重做
        assert statuses["算指标"] == "pending"
        assert shown["steps"][3]["rejected"] == 2
        assert shown["position"]["step"] == 2
    finally:
        await close(runner)
        await fake.close()


async def test_stopped_to_ask_the_user(db):
    """AT-INV-23：勾稽不平，不重做，停下来问。"""
    checks = [dict(c) for c in RECONCILE["checks"]]
    checks[0]["unbalanced"] = 2
    fake, runner, sid, _, task_id = await run(
        db, [SCOPE, PROFILE, RECONCILE | {"checks": checks}]
    )
    try:
        shown = await view(db, task_id)
        assert rail(shown)[:4] == [
            (1, "accepted", 1, 0),
            (2, "accepted", 1, 0),
            (3, "waiting", 1, 1),
            (4, "pending", 0, 0),
        ]
        sheet = shown["task"]
        assert sheet["state"] == "WAITING" and sheet["waiting_reason"] == "INPUT"
        assert sheet["active_interaction_id"]
        assert sheet["ended_at"] is None
        assert shown["position"] == {"step": 3, "of": 7, "title": "勾稽", "left": 5}
        (attempt,) = (await view(db, task_id, 3))["attempts"]
        failed = [c for c in attempt["checks"] if c["pass"] is False]
        assert [(c["label"], c["message"]) for c in failed] == [
            ("每条勾稽规则都平", "有勾稽规则不平，不往下算")
        ]

        async with db() as s:
            repo = WorkbenchRepository(s)
            token = next(
                e["payload"]["token"]
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "interaction/opened"
            )
            await Ledger(repo).respond_interaction(
                sheet["active_interaction_id"],
                token=token,
                response={"decision": "stop"},
                owner_actor_id=OWNER,
            )
        stopped = await view(db, task_id)
        assert stopped["task"]["state_word"] == "失败"
        assert [s["status"] for s in stopped["steps"]] == [
            "accepted",
            "accepted",
            "failed",
            "not_reached",
            "not_reached",
            "not_reached",
            "not_reached",
        ]
        assert stopped["task"]["budget"]["used"] == "0.03"  # 已花的如实
    finally:
        await close(runner)
        await fake.close()


async def test_a_company_without_data(db):
    """AT-INV-24 的后端一半：第 2 步的验收里写明未入库，没有往下做。"""
    missing = PROFILE | {
        "dataset": None,
        "not_ingested": True,
        "data_version": "",
        "periods": [],
    }
    fake, runner, *_, task_id = await run(db, [SCOPE, missing, missing])
    try:
        shown = await view(db, task_id)
        assert [s["status"] for s in shown["steps"]][:3] == [
            "accepted",
            "waiting",
            "pending",
        ]
        assert shown["steps"][1]["times"] == 1  # 没有数据不重做
        assert shown["task"]["data"] is None  # 还不知道
        last = (await view(db, task_id, 2))["attempts"][-1]
        assert ("有对应的数据集", False, "这家公司未入库：没有对应的数据集") in [
            (c["label"], c["pass"], c["message"]) for c in last["checks"]
        ]
    finally:
        await close(runner)
        await fake.close()


async def test_nothing_of_the_method_is_in_the_view(db):
    """AT-INV-19"""
    fake, runner, *_, task_id = await run(db, ALL)
    try:
        shown = json.dumps(await view(db, task_id), default=str, ensure_ascii=False)
        for n in range(1, 8):
            shown += json.dumps(
                await view(db, task_id, n), default=str, ensure_ascii=False
            )
        assert "method" not in shown
        assert "Reply with ONE JSON" not in shown
        assert "Advisor step" not in shown
    finally:
        await close(runner)
        await fake.close()


async def test_only_the_owner_sees_it(db):
    """F-PROJ-08、AT-INV-15"""
    fake, runner, *_, task_id = await run(db, ALL[:1])
    try:
        with pytest.raises(NotFound):
            await view(db, task_id, owner=STRANGER)
        with pytest.raises(NotFound):
            await view(db, task_id, 1, owner=STRANGER)
        for number in (0, 8):
            with pytest.raises(NotFound):
                await view(db, task_id, number)
    finally:
        await close(runner)
        await fake.close()


async def test_going_back_is_allowed_once_then_the_expert_asks(db):
    """所有者 2026-10-04 定：退回前面最多 1 次。回来仍然不过，专家不再自己来回，停下来问。"""
    empty = FACTS | {"table": []}
    fake, runner, sid, _, task_id = await run(
        db, [SCOPE, PROFILE, RECONCILE, empty, empty, PROFILE, RECONCILE, empty]
    )
    try:
        shown = await view(db, task_id)
        assert shown["task"]["state"] == "WAITING"
        statuses = {s["title"]: s["status"] for s in shown["steps"]}
        assert statuses["取数"] == "waiting"
        assert statuses["数据摸底"] == "accepted" and statuses["勾稽"] == "accepted"
        assert shown["steps"][3]["rejected"] == 3
        assert shown["steps"][3]["after_rejection"] == {
            "reworks": 1,
            "then": "back",
            "back_to": 2,
            "backs": 1,
            "text": "重做，最多 1 次；仍不过，退回第 2 步，最多 1 次；仍不过，停下来问你",
        }
        async with db() as s:
            repo = WorkbenchRepository(s)
            backs = await repo.list_task_events(task_id, types=("step/went_back",))
            waiting = await repo.list_interactions(session_id=sid, status="pending")
        assert [e["payload"] for e in backs] == [
            {"step_id": "extract", "back_to": "profile", "time": 1, "of": 1}
        ]
        assert len(waiting) == 1 and waiting[0]["kind"] == "input"
        assert "已经退回前面重做过 1 次" in waiting[0]["prompt"]["question"]
        assert waiting[0]["prompt"]["subject"]["backs"] == 1
    finally:
        await close(runner)
        await fake.close()
