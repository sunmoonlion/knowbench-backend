"""等我决定的事：跨对话的清单与审查面的五要素（investment-expert.md 第八节；AT-INV-23）。"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sample_replies import ALL, METRICS, PROFILE, RECONCILE, SCOPE
from sqlalchemy import text
from test_workbench_advisor_db import close
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_step_view_db import run

from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.ledger import Ledger
from app.domain.workbench.errors import NotFound
from app.infrastructure.workbench.repository import WorkbenchRepository

STRANGER = str(uuid.uuid4())


def unbalanced():
    checks = [dict(c) for c in RECONCILE["checks"]]
    checks[0]["unbalanced"] = 2
    return RECONCILE | {"checks": checks}


async def desk(db, call, **kw):
    async with db() as s:
        return await getattr(ExpertDesk(WorkbenchRepository(s)), call)(**kw)


async def test_the_five_parts_of_a_review(db):
    fake, runner, sid, _, task_id = await run(db, [SCOPE, PROFILE, unbalanced()])
    try:
        (waiting,) = await desk(db, "waiting_for_me", owner_actor_id=OWNER)
        # 摘要：在哪、为什么、哪几条没过、什么时候过期。没有选项，也没有令牌在哪
        assert set(waiting) == {
            "interaction_id",
            "kind",
            "status",
            "where",
            "why",
            "failed",
            "missing_data",
            "expires_at",
            "created_at",
        }
        assert waiting["missing_data"] is None
        assert waiting["where"]["task"]["expert"] == "财报体检"
        assert waiting["where"]["task"]["id"] == task_id
        assert waiting["where"]["step"] == {"index": 3, "title": "勾稽"}
        assert waiting["where"]["about"] == "验收没有通过"
        assert waiting["where"]["conversation"]["id"] == sid
        assert waiting["where"]["project"]["title"]
        assert (
            waiting["why"] == "这一步没有通过验收。这一步不通过时专家不重做，直接问你"
        )
        assert waiting["failed"] == [
            {"label": "每条勾稽规则都平", "message": "有勾稽规则不平，不往下算"}
        ]

        full = await desk(
            db, "review", interaction_id=waiting["interaction_id"], owner_actor_id=OWNER
        )
        assert full["status"] == "pending"
        assert [(o["id"], o["consequence"]) for o in full["pending"]["options"]] == [
            ("rework", "专家重做这一步。重做要再花钱，花了多少顶部实时显示"),
            ("stop", "专家停下，把做完的 2 步交回给你。已花的不退"),
        ]
        assert full["pending"]["unknowns"] == [
            "all_equal(checks): 有勾稽规则不平，不往下算"
        ]
        assert '"unbalanced": 2' in full["subject"]["returned"]
        assert full["subject"]["target_state_version"] is not None
        left = full["validity"]["seconds_left"]
        assert 71 * 3600 < left <= 72 * 3600
        assert "不等于同意" in full["validity"]["after_expiry"]
        assert full["decision"] == {
            "decided": False,
            "at": None,
            "by": None,
            "chosen": None,
            "amount": None,
            "cancelled_because": None,
        }
        assert "token" not in json.dumps(full, default=str)

        # 令牌在开出这件事的那条事件里：按给出的位置取得到
        where = full["opened_event"]
        async with db() as s:
            repo = WorkbenchRepository(s)
            (event,) = await repo.list_events(
                session_id=where["session_id"],
                after_cursor=where["cursor"] - 1,
                limit=1,
            )
            assert event["type"] == "interaction/opened"
            await Ledger(repo).respond_interaction(
                full["interaction_id"],
                token=event["payload"]["token"],
                response={"decision": "stop"},
                owner_actor_id=OWNER,
            )

        assert await desk(db, "waiting_for_me", owner_actor_id=OWNER) == []
        after = await desk(
            db, "review", interaction_id=full["interaction_id"], owner_actor_id=OWNER
        )
        assert after["status"] == "consumed"
        assert after["decision"]["decided"] is True
        assert after["decision"]["chosen"] == "stop"
        assert after["decision"]["by"] == OWNER
        assert after["decision"]["at"] is not None

        home = await desk(db, "overview", owner_actor_id=OWNER)
        assert home["waiting"] == [] and home["running"] == []
        (returned,) = home["returned"]
        assert returned["state_word"] == "失败"
        assert returned["reason"]["by"] == "user stopped"
        assert returned["reason_text"] == "专家停下来问你，你选择了停止"
        assert returned["budget"]["used"] == "0.03"
        assert returned["position"]["step"] == 3
    finally:
        await close(runner)
        await fake.close()


async def test_a_step_that_used_up_its_tries(db):
    empty = METRICS | {"table": []}
    fake, runner, *_, task_id = await run(db, [*ALL[:4], empty, empty, empty])
    try:
        home = await desk(db, "overview", owner_actor_id=OWNER)
        (waiting,) = home["waiting"]
        assert waiting["where"]["step"] == {"index": 5, "title": "算指标"}
        assert waiting["why"] == "这一步做了 3 次，都没有通过验收"
        assert waiting["failed"] == [
            {"label": "指标表不是空的", "message": "指标表为空"}
        ]
        assert waiting["where"]["task"]["id"] == task_id
        # 等我决定的只列在「等我决定」里，不在「进行中」里再列一遍
        assert home["running"] == []
        assert home["returned"] == []
    finally:
        await close(runner)
        await fake.close()


async def test_someone_else_sees_nothing(db):
    fake, runner, *_ = await run(db, [SCOPE, PROFILE, unbalanced()])
    try:
        (mine,) = await desk(db, "waiting_for_me", owner_actor_id=OWNER)
        assert await desk(db, "waiting_for_me", owner_actor_id=STRANGER) == []
        home = await desk(db, "overview", owner_actor_id=STRANGER)
        assert home == {"waiting": [], "running": [], "returned": []}
        with pytest.raises(NotFound):
            await desk(
                db,
                "review",
                interaction_id=mine["interaction_id"],
                owner_actor_id=STRANGER,
            )
        with pytest.raises(NotFound):
            await desk(
                db, "review", interaction_id=str(uuid.uuid4()), owner_actor_id=OWNER
            )
    finally:
        await close(runner)
        await fake.close()


async def test_a_review_past_its_time_is_said_to_be_expired(db):
    fake, runner, *_ = await run(db, [SCOPE, PROFILE, unbalanced()])
    try:
        (mine,) = await desk(db, "waiting_for_me", owner_actor_id=OWNER)
        async with db() as s:
            await s.execute(
                text("update workbench_interactions set expires_at = :t where id = :i"),
                {
                    "t": datetime.now(UTC) - timedelta(minutes=1),
                    "i": mine["interaction_id"],
                },
            )
            await s.commit()
        assert await desk(db, "waiting_for_me", owner_actor_id=OWNER) == []
        full = await desk(
            db, "review", interaction_id=mine["interaction_id"], owner_actor_id=OWNER
        )
        assert full["status"] == "expired"
        assert full["validity"]["seconds_left"] == 0
        assert full["decision"]["decided"] is False
    finally:
        await close(runner)
        await fake.close()


async def test_a_command_waiting_for_approval_in_a_conversation(db):
    """用户自己工作时 Codex 要批准：没有委托、没有步骤，用同一面的简版。"""
    fake, runner, sid, *_ = await run(db, ALL)
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                iid = await repo.insert_interaction(
                    session_id=sid,
                    task_id=None,
                    attempt_id=None,
                    kind="tool_approval",
                    prompt={
                        "title": "命令需要你批准",
                        "question": "Codex 请求执行超出沙箱的动作",
                        "options": [
                            {"id": "accept", "label": "允许一次"},
                            {"id": "acceptForSession", "label": "本会话都允许"},
                            {"id": "decline", "label": "拒绝"},
                        ],
                        "subject": {
                            "request_id": "sb:7",
                            "command": "touch /etc/x",
                            "cwd": "/home/u/research/proj",
                        },
                        "evidence": [],
                        "unknowns": [],
                    },
                    subject_digest=None,
                    token="t" * 40,
                    target_state_version=None,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
        (waiting,) = await desk(db, "waiting_for_me", owner_actor_id=OWNER)
        assert waiting["interaction_id"] == iid
        assert waiting["where"]["task"] is None and waiting["where"]["step"] is None
        assert waiting["where"]["about"] == "命令要你批准"
        assert waiting["failed"] == []
        # 专家首页只列委托的待办；对话里的批准在对话页答
        assert (await desk(db, "overview", owner_actor_id=OWNER))["waiting"] == []
        full = await desk(db, "review", interaction_id=iid, owner_actor_id=OWNER)
        assert full["subject"]["command"] == "touch /etc/x"
        assert full["subject"]["cwd"] == "/home/u/research/proj"
        assert [o["id"] for o in full["pending"]["options"]] == [
            "accept",
            "acceptForSession",
            "decline",
        ]
        assert all(o["consequence"] for o in full["pending"]["options"])
        assert "白名单" in full["pending"]["options"][0]["consequence"]
        assert full["opened_event"]["cursor"] is None  # 这一条是直接插的，没有事件
    finally:
        await close(runner)
        await fake.close()


async def test_stopped_because_the_company_has_no_data(db):
    """AT-INV-24"""
    missing = PROFILE | {
        "dataset": None,
        "not_ingested": True,
        "data_version": "",
        "periods": [],
    }
    fake, runner, sid, _, task_id = await run(db, [SCOPE, missing])
    try:
        (waiting,) = await desk(db, "waiting_for_me", owner_actor_id=OWNER)
        assert waiting["where"]["step"] == {"index": 2, "title": "数据摸底"}
        assert waiting["why"] == "这家公司还没有入库。没有数据，专家不往下做，也不重做"
        assert waiting["missing_data"]["security_code"] == SCOPE["security_code"]
        assert {
            "label": "有对应的数据集",
            "message": "这家公司未入库：没有对应的数据集",
        } in (waiting["failed"])
        full = await desk(
            db, "review", interaction_id=waiting["interaction_id"], owner_actor_id=OWNER
        )
        assert full["pending"]["title"] == "这家公司还没有数据"
        assert [(o["id"], o["label"]) for o in full["pending"]["options"]] == [
            ("rework", "数据已经入库了，接着做"),
            ("stop", "停止"),
        ]
        async with db() as s:
            paper = await ExpertDesk(WorkbenchRepository(s)).dossier(
                task_id, owner_actor_id=OWNER
            )
        assert paper["head"] == {"kind": "no_data", "text": "这家公司没有数据"}
    finally:
        await close(runner)
        await fake.close()
