"""固定专家按计划回执执行的等价验收（SDD 0050 第五节）。

同一位固定专家、同一份脚本，开关关着跑一次、开着跑一次，逐项比：模型每一轮看到的输入（逐字）、
每次尝试的步骤与结果与用到的输入版本与花费、每件成果的内容、委托的状态与游标与预算与指向的成果、
事件流、问用户的问题、交回。只有两处按设计不同、不比：开着时多一件计划回执；交回的「已核实」
清单变成已接受的各步结果（所有者 2026-11-06 接受）。每次跑都不同的编号、时间换成占位。
"""

import json
import re
from decimal import Decimal
from typing import Any

import pytest
from test_workbench_advisor_db import (
    ANSWER,
    PLAN,
    ScriptedAppServer,
    close,
    drive,
    seed,
)
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench import advisor as advisor_module
from app.application.workbench.ledger import Ledger
from app.bootstrap.workbench import build_runner
from app.domain.workbench.method_plan import PLAN_ARTIFACT
from app.domain.workbench.packs import BUILTIN_PACKS, SMOKE, AskBefore
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository

_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_THREAD = re.compile(r"thread-[0-9a-f]{8}")
_FOLDER = re.compile(r"对话-[0-9a-f]{8}")  # 成稿目录按会话编号起名
_TURN = re.compile(r"turn-[0-9a-f]{6}")  # 假 app-server 每轮随机起的编号
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}-\d{6}")  # 成稿目录里委托开始的时刻（到秒）
_PLACEHOLDERS = (
    (_ID, "<id>"),
    (_THREAD, "<thread>"),
    (_TURN, "<turn>"),
    (_FOLDER, "对话-<id>"),
    (_STAMP, "<time>"),
)
_VOLATILE = {
    "created_at",
    "updated_at",
    "started_at",
    "ended_at",
    "at",
    "token",
    "expires_at",
    "deadline",
    "digest",  # 交回的摘要随「已核实」清单变；成果内容另比
}
_RELAYED = {"item", "thread", "turn"}  # app-server 的通知，runner 照转
BAD = json.dumps({"answer": "", "citations": []})
ADVICE = json.dumps(
    {"answer": "强烈买入，目标价 100 元", "citations": ["x"], "conclusion": ""}
)


def _norm(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list | tuple):
        return [_norm(v) for v in value]
    if isinstance(value, str):
        for pattern, placeholder in _PLACEHOLDERS:
            value = pattern.sub(placeholder, value)
        return value
    if isinstance(value, Decimal):
        return str(value)
    return _ID.sub("<id>", str(value)) if value.__class__.__name__ == "UUID" else value


def _first_diff(key: str, off: Any, on: Any) -> str:
    """失败时说清第一处不同在哪。"""
    if isinstance(off, list) and isinstance(on, list):
        for i, (a, b) in enumerate(zip(off, on, strict=False)):
            if a != b:
                return f"{key}[{i}]\n off: {a}\n on:  {b}"
        return f"{key}: length {len(off)} vs {len(on)}"
    return f"{key}\n off: {off}\n on:  {on}"


def _inputs(attempt: dict[str, Any]) -> list[tuple[str, int]]:
    return [(i["name"], i["version"]) for i in attempt["input_artifact_versions"]]


async def _snapshot(db, sid: str, tid: str, fake: ScriptedAppServer) -> dict[str, Any]:
    async with db() as s:
        repo = WorkbenchRepository(s)
        task = await repo.get_task(tid)
        attempts = await repo.list_attempts(tid)
        artifacts = {}
        for a in await repo.list_artifacts(tid):
            if a["name"] == PLAN_ARTIFACT:
                continue
            full = await repo.get_artifact(
                task_id=tid, name=a["name"], version=a["version"]
            )
            assert full is not None
            content = full["content"]
            if a["name"] == "handback" and isinstance(content, dict):
                content = {k: v for k, v in content.items() if k != "verified"}
            artifacts[(a["name"], a["version"])] = (a["kind"], _norm(content))
        names = {str(a["id"]): a["name"] for a in await repo.list_artifacts(tid)}
        stream = [
            (e["type"], _norm(e["payload"]))
            for e in await repo.list_events(session_id=sid)
        ]
        # 两路各自有序：工作台自己写的，和 runner 从 app-server 转录的（模型回了什么、turn
        # 结束）。两路同时写，谁先落库每次可能不同，不是行为差别；各自逐条比，不比交错
        relayed = [e for e in stream if e[0].split("/", 1)[0] in _RELAYED]
        events = [e for e in stream if e[0].split("/", 1)[0] not in _RELAYED]
        interactions = [
            (i["kind"], i["status"], _norm(i["prompt"]), _norm(i.get("response")))
            for i in await repo.list_interactions(session_id=sid, status=None)
        ]
    return {
        "turn_inputs": _norm(fake.turn_inputs),
        "task": {
            "state": task["state"],
            "waiting_reason": task["waiting_reason"],
            "current_step": task["current_step"],
            "budget": _norm(task["budget"]),
            "terminal_reason": _norm(task.get("terminal_reason")),
            "result": names.get(str(task.get("terminal_result_ref"))),
            "contract": _norm(task.get("acceptance_contract")),
        },
        "attempts": [
            (
                a["step_id"],
                a["step_version"],
                a["status"],
                a["failure_code"],
                _inputs(a),
                _norm(a["budget_allocated"]),
                _norm(a["budget_consumed"]),
                [(o["name"], o["version"]) for o in a["output_artifacts"]],
            )
            for a in attempts
        ],
        "artifacts": artifacts,
        "events": events,
        "relayed": relayed,
        "interactions": interactions,
        "planned": task["accepted_plan_version"],
    }


async def _answer(db, sid: str, sb: str, tid: str, response: dict[str, Any]) -> None:
    async with db() as s:
        repo = WorkbenchRepository(s)
        it = (await repo.list_interactions(session_id=sid, status="pending"))[0]
        token = next(
            e["payload"]["token"]
            for e in await repo.list_events(session_id=sid)
            if e["type"] == "interaction/opened"
            and e["payload"].get("interaction_id") in (None, str(it["id"]))
        )
        await Ledger(repo).respond_interaction(
            str(it["id"]), token=token, response=response, owner_actor_id=OWNER
        )
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sb,
                kind="task.drive",
                payload={"task_id": tid},
            )


async def _cancel(db, tid: str) -> None:
    async with db() as s:
        await Ledger(WorkbenchRepository(s)).request_cancel(tid, owner_actor_id=OWNER)


def _fresh_runner(db):
    """进程重启：新起一个 runner，从库里接着做（不带上一个进程的内存）。"""
    return build_runner(
        db, publisher=RedisPublisher(None, "t"), runner_id="r", poll_seconds=0.05
    )


async def _redrive(db, sid: str, sb: str, tid: str) -> None:
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.enqueue_command(
                session_id=sid,
                sandbox_id=sb,
                kind="task.drive",
                payload={"task_id": tid},
            )


async def _run(db, monkeypatch, *, on: bool, replies, budget, answers, crash=None):
    """answers 里每一项在一次驾驶之后做：dict 是答复待答的问题，"cancel" 是用户叫停，
    "restart" 是换一个新进程接着驾驶。crash 是步骤 id：这一步刚开始时进程断掉一次。"""
    original = advisor_module.Advisor.__init__

    def init(self, *a, **kw):
        kw["fixed_as_plan"] = on
        original(self, *a, **kw)

    monkeypatch.setattr(advisor_module.Advisor, "__init__", init)
    append = WorkbenchRepository.append_event
    crashed: list[str] = []
    if crash is not None:

        async def broken(self, **kwargs):
            if (
                not crashed
                and kwargs["event_type"] == "step/started"
                and kwargs["payload"].get("step_id") == crash
            ):
                crashed.append(crash)
                raise RuntimeError("process died")
            return await append(self, **kwargs)

        monkeypatch.setattr(WorkbenchRepository, "append_event", broken)
    fake = ScriptedAppServer(list(replies))
    await fake.start()
    runner = None
    try:
        runner, sid, sb, tid = await seed(db, fake, budget=budget)
        await drive(runner)
        for action in answers:
            if action == "cancel":
                await _cancel(db, tid)
                await _redrive(db, sid, sb, tid)
            elif action == "restart":
                await close(runner)
                monkeypatch.setattr(WorkbenchRepository, "append_event", append)
                runner = _fresh_runner(db)
                await runner.run_once()  # 接管沙箱、对账
                await _redrive(db, sid, sb, tid)
            else:
                await _answer(db, sid, sb, tid, action)
            await drive(runner)
        snapshot = await _snapshot(db, sid, tid, fake)
        snapshot["crashed"] = list(crashed)
        return snapshot
    finally:
        if runner is not None:
            await close(runner)
        await fake.close()
        monkeypatch.setattr(WorkbenchRepository, "append_event", append)
        monkeypatch.setattr(advisor_module.Advisor, "__init__", original)


SCENARIOS = {
    "happy": ([PLAN, ANSWER], "10", []),
    "rework_once": (["sorry, no json here", PLAN, ANSWER], "10", []),
    "reworks_exhausted_then_resumed": (
        [PLAN, BAD, BAD, ANSWER],
        "10",
        [{"decision": "rework"}],
    ),
    "human_stop": ([PLAN, BAD, BAD], "10", [{"decision": "stop"}]),
    "budget_stop_then_topup": (
        [PLAN, ANSWER],
        "0.055",
        [{"decision": "topup", "amount": "1"}],
    ),
    "positioning_advice_blocked": ([PLAN, ADVICE, ADVICE], "10", []),
    "user_stops_while_waiting": ([PLAN, BAD, BAD], "10", ["cancel"]),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_fixed_expert_behaves_the_same_on_the_plan_path(db, monkeypatch, name):
    replies, budget, answers = SCENARIOS[name]
    off = await _run(
        db, monkeypatch, on=False, replies=replies, budget=budget, answers=answers
    )
    on = await _run(
        db, monkeypatch, on=True, replies=replies, budget=budget, answers=answers
    )
    assert off.pop("planned") is None
    assert on.pop("planned") == 1  # 开着时确实走了回执
    for key in off:
        assert on[key] == off[key], _first_diff(key, off[key], on[key])


async def test_send_back_to_an_earlier_step_is_the_same(db, monkeypatch):
    """作者让第二步不过时退回第一步：退回、重做、谱系都和旧路一样。"""
    back = SMOKE.model_copy(
        update={
            "workflow": (
                SMOKE.workflow[0],
                SMOKE.workflow[1].model_copy(
                    update={
                        "on_reject": "back",
                        "back_to": "plan",
                        "max_backs": 1,
                        "max_reworks": 0,  # 不先重做，直接退回
                    }
                ),
            )
        }
    )
    monkeypatch.setitem(BUILTIN_PACKS, "SMOKE", back)
    replies = [PLAN, BAD, PLAN, ANSWER]
    off = await _run(
        db, monkeypatch, on=False, replies=replies, budget="10", answers=[]
    )
    on = await _run(db, monkeypatch, on=True, replies=replies, budget="10", answers=[])
    assert off.pop("planned") is None and on.pop("planned") == 1
    assert [a[0] for a in off["attempts"]] == ["plan", "answer", "plan", "answer"]
    for key in off:
        assert on[key] == off[key], _first_diff(key, off[key], on[key])


def _variant(**answer_changes):
    return SMOKE.model_copy(
        update={
            "workflow": (
                SMOKE.workflow[0],
                SMOKE.workflow[1].model_copy(update=answer_changes),
            )
        }
    )


async def _same(db, monkeypatch, *, replies, answers, crash=None, budget="10"):
    off = await _run(
        db,
        monkeypatch,
        on=False,
        replies=replies,
        budget=budget,
        answers=answers,
        crash=crash,
    )
    on = await _run(
        db,
        monkeypatch,
        on=True,
        replies=replies,
        budget=budget,
        answers=answers,
        crash=crash,
    )
    assert off.pop("planned") is None and on.pop("planned") == 1
    for key in off:
        assert on[key] == off[key], _first_diff(key, off[key], on[key])
    return off


ASK = AskBefore(id="scope", question="只看哪一年？")


async def test_a_question_before_the_step_is_the_same(db, monkeypatch):
    """开步前先问用户：问的时候停下等，答了接着做，答案进这一步的输入。"""
    monkeypatch.setitem(BUILTIN_PACKS, "SMOKE", _variant(ask_before=(ASK,)))
    off = await _same(
        db,
        monkeypatch,
        replies=[PLAN, ANSWER],
        answers=[{"answers": {"scope": "2024 年"}}],
    )
    assert off["task"]["state"] == "SUCCEEDED"
    assert [i[0] for i in off["interactions"]] == ["question"]
    assert "2024 年" in off["turn_inputs"][-1]


async def test_stopping_while_the_question_waits_is_the_same(db, monkeypatch):
    monkeypatch.setitem(BUILTIN_PACKS, "SMOKE", _variant(ask_before=(ASK,)))
    off = await _same(db, monkeypatch, replies=[PLAN, ANSWER], answers=["cancel"])
    assert off["task"]["state"] == "CANCELLED"
    assert len(off["turn_inputs"]) == 1  # 第二步没开过 turn


async def test_a_crash_at_a_step_start_recovers_the_same(db, monkeypatch):
    """第二步刚开始时进程断了，新进程接着做：两条路断在同一处、恢复成同一个样子。"""
    off = await _same(
        db,
        monkeypatch,
        replies=[PLAN, ANSWER],
        answers=["restart"],
        crash="answer",
    )
    assert off["crashed"] == ["answer"]


async def test_outputs_named_apart_from_their_steps_are_the_same(db, monkeypatch):
    """产出名和步骤名不同（第一步 plan 产出 draft），第二步读 draft、不过退回 plan。"""
    renamed = SMOKE.model_copy(
        update={
            "workflow": (
                SMOKE.workflow[0].model_copy(update={"output_artifact": "draft"}),
                SMOKE.workflow[1].model_copy(
                    update={
                        "input_refs": ("draft",),
                        "on_reject": "back",
                        "back_to": "plan",
                        "max_backs": 1,
                        "max_reworks": 0,
                    }
                ),
            )
        }
    )
    monkeypatch.setitem(BUILTIN_PACKS, "SMOKE", renamed)
    off = await _same(db, monkeypatch, replies=[PLAN, BAD, PLAN, ANSWER], answers=[])
    assert off["task"]["state"] == "SUCCEEDED"
    assert [a[0] for a in off["attempts"]] == ["plan", "answer", "plan", "answer"]
    assert off["attempts"][-1][4] == [("draft", 2)]  # 退回后读的是第一步新的那一版
    assert {name for name, _ in off["artifacts"]} == {"draft", "answer", "handback"}
