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
from app.domain.workbench.method_plan import PLAN_ARTIFACT
from app.domain.workbench.packs import BUILTIN_PACKS, SMOKE
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
        events = [
            (e["type"], _norm(e["payload"]))
            for e in await repo.list_events(session_id=sid)
        ]
        interactions = [
            (i["kind"], i["status"], _norm(i["prompt"]))
            for i in await repo.list_interactions(session_id=sid)
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


async def _run(db, monkeypatch, *, on: bool, replies, budget, answers):
    original = advisor_module.Advisor.__init__

    def init(self, *a, **kw):
        kw["fixed_as_plan"] = on
        original(self, *a, **kw)

    monkeypatch.setattr(advisor_module.Advisor, "__init__", init)
    fake = ScriptedAppServer(list(replies))
    await fake.start()
    runner = None
    try:
        runner, sid, sb, tid = await seed(db, fake, budget=budget)
        await drive(runner)
        for response in answers:
            await _answer(db, sid, sb, tid, response)
            await drive(runner)
        return await _snapshot(db, sid, tid, fake)
    finally:
        if runner is not None:
            await close(runner)
        await fake.close()
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
        assert on[key] == off[key], key


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
        assert on[key] == off[key], key
