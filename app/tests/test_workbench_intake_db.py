"""受理关口（账 130）：第一步先判问题归不归这位专家。不归就停下问用户照做还是不做，不报错、不返工。

演示时出过洋相：用户问了无关问题，模型照着步骤硬做，验收不过，最后报错。
"""

from __future__ import annotations

import json

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
from test_workbench_ledger_db import db as db

from app.application.workbench.acceptance import out_of_scope
from app.application.workbench.ledger import Ledger
from app.domain.workbench import packs
from app.domain.workbench.packs import SMOKE, Scope
from app.domain.workbench.review_view import consequence, why
from app.domain.workbench.states import AttemptState, TaskState
from app.domain.workbench.step_view import reason_text
from app.infrastructure.workbench.repository import WorkbenchRepository

INTAKE = "## First: is this question for this expert?"
DECLINE = json.dumps(
    {
        "out_of_scope": True,
        "reason": "这是在问天气，不是这位专家做的事",
        "suggestion": "直接在对话里问",
    },
    ensure_ascii=False,
)


@pytest.fixture
def listed_smoke(monkeypatch):
    """机制烟测本来不设关口（只给我们自己用）；这里当成上架的专家来测。"""
    monkeypatch.setitem(
        packs.BUILTIN_PACKS, "SMOKE", SMOKE.model_copy(update={"internal": False})
    )


async def answer(db, sid, sb, t, decision):
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
            response={"decision": decision},
            owner_actor_id=OWNER,
        )
        if r["state"] == TaskState.QUEUED:
            async with repo.transaction():
                await repo.enqueue_command(
                    session_id=sid,
                    sandbox_id=sb,
                    kind="task.drive",
                    payload={"task_id": t},
                )
        return r


async def test_unrelated_question_stops_and_asks_then_proceeds(db, listed_smoke):
    fake = ScriptedAppServer([DECLINE, PLAN, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(t)
            assert task["state"] == TaskState.WAITING
            assert task["waiting_reason"] == "INPUT"
            it = (await repo.list_interactions(session_id=sid, status="pending"))[0]
            subject = it["prompt"]["subject"]
            assert subject["out_of_scope"] is True and subject["step_id"] == "plan"
            assert "天气" in it["prompt"]["question"]
            assert [o["id"] for o in it["prompt"]["options"]] == ["proceed", "stop"]
            (attempt,) = await repo.list_attempts(t)
            # 不算做过、不算返工
            assert attempt["status"] == AttemptState.CANCELLED
            assert attempt["failure_code"] == "out_of_scope"
            assert not [
                e
                for e in await repo.list_events(session_id=sid)
                if e["type"] == "step/rejected"
            ]
        assert INTAKE in fake.turn_inputs[0]
        assert (await answer(db, sid, sb, t, "proceed"))["state"] == TaskState.QUEUED
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert (await repo.get_task(t))["state"] == TaskState.SUCCEEDED
            assert [a["step_id"] for a in await repo.list_attempts(t)] == [
                "plan",
                "plan",
                "answer",
            ]
        # 用户说了照做：不再判；后面的步骤也不判
        assert all(INTAKE not in text for text in fake.turn_inputs[1:])
        await close(runner)
    finally:
        await fake.close()


async def test_unrelated_question_stopped_is_cancelled_not_failed(db, listed_smoke):
    fake = ScriptedAppServer([DECLINE])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        r = await answer(db, sid, sb, t, "stop")
        assert r["state"] == TaskState.CANCELLED
        async with db() as s:
            task = await WorkbenchRepository(s).get_task(t)
            assert task["state"] == TaskState.CANCELLED
        await close(runner)
    finally:
        await fake.close()


async def test_question_in_scope_runs_as_before(db, listed_smoke):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        async with db() as s:
            assert (await WorkbenchRepository(s).get_task(t))[
                "state"
            ] == TaskState.SUCCEEDED
        assert INTAKE in fake.turn_inputs[0]
        assert INTAKE not in fake.turn_inputs[1]
        await close(runner)
    finally:
        await fake.close()


async def test_internal_expert_has_no_intake(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    try:
        runner, sid, sb, t = await seed(db, fake)
        await drive(runner)
        assert all(INTAKE not in text for text in fake.turn_inputs)
        await close(runner)
    finally:
        await fake.close()


def test_out_of_scope_reply_is_read_only_when_it_says_so():
    assert out_of_scope(DECLINE) == {
        "reason": "这是在问天气，不是这位专家做的事",
        "suggestion": "直接在对话里问",
    }
    assert out_of_scope("```json\n" + DECLINE + "\n```") is not None
    assert out_of_scope(PLAN) is None
    assert out_of_scope(json.dumps({"out_of_scope": "true"})) is None
    assert out_of_scope(json.dumps({"out_of_scope": False})) is None
    assert out_of_scope("not json") is None
    assert out_of_scope(None) is None
    assert out_of_scope(json.dumps({"out_of_scope": True})) == {
        "reason": "",
        "suggestion": "",
    }


def test_intake_scope_comes_from_the_six_answers_unless_written():
    assert SMOKE.intake_scope() is None  # 只给我们自己用的不设关口
    # 内置的财务分析与预测 10-18 撤下、标成内部；这里只借它的六问核推法
    fc = packs.FIN_FORECAST.model_copy(update={"internal": False})
    scope = fc.intake_scope()
    assert scope is not None
    assert scope.does == fc.answers["解决什么"]
    assert scope.does_not == fc.answers["不解决什么"]
    bare = SMOKE.model_copy(update={"internal": False, "answers": {}})
    assert bare.intake_scope() == Scope(does=f"{SMOKE.name}；{SMOKE.tagline}")
    written = Scope(does="只答天气", does_not="别的", elsewhere="对话里问")
    assert bare.model_copy(update={"scope": written}).intake_scope() == written


def test_builtin_experts_are_withdrawn_from_users():
    """待办账 111、数据拓扑：平台不带自己的投资逻辑与数据，内置专家用户看不到、请不了。"""
    import pytest

    from app.application.workbench.catalog import builtin_on_offer, builtin_on_view
    from app.domain.workbench.errors import NoSuchExpert

    assert packs.listed_packs() == []
    for pid in ("DATA_QUERY", "FIN_REVIEW", "FIN_FORECAST"):
        assert packs.find_pack(pid) is not None  # 历史委托还能显示
        for look in (builtin_on_view, builtin_on_offer):
            with pytest.raises(NoSuchExpert):
                look(pid)


def test_every_listed_expert_has_an_intake():
    for pack in packs.listed_packs():
        scope = pack.intake_scope()
        assert scope is not None and scope.does.strip(), pack.profile_id


def test_intake_words_for_the_user():
    assert "不归" in why("input", None, 0, out_of_scope=True)
    assert "不再判" in consequence(
        "proceed", "input", reserve=None, done=0, out_of_scope=True
    )
    assert "不再花钱" in consequence(
        "stop", "input", reserve=None, done=0, out_of_scope=True
    )
    assert "不归" in reason_text(
        TaskState.CANCELLED, {"by": "user", "code": "out_of_scope"}
    )
