"""底稿（PRD/apps/investment-expert.md 第七节；AT-INV-26 至 28）。"""

from __future__ import annotations

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
)
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
from test_workbench_review_db import unbalanced
from test_workbench_step_view_db import run

from app.application.workbench.expert_desk import ExpertDesk
from app.domain.workbench.errors import NotFound
from app.infrastructure.workbench.repository import WorkbenchRepository


async def dossier(db, task_id, owner=OWNER):
    async with db() as s:
        return await ExpertDesk(WorkbenchRepository(s)).dossier(
            task_id, owner_actor_id=owner
        )


def blocks(view):
    return {b["key"]: b for s in view["sections"] for b in s["blocks"]}


async def test_a_finished_review_reads_like_a_working_paper(db):
    fake, runner, *_, task_id = await run(db, ALL)
    try:
        view = await dossier(db, task_id)
        assert view["head"] == {"kind": "done", "text": "专家做完了"}
        assert view["task"]["question"]
        assert view["task"]["state_word"] == "已完成"
        assert [(s["key"], s["title"]) for s in view["sections"]] == [
            ("answer", "回答"),
            ("data", "数据"),
            ("verified", "专家验过什么"),
            ("notes", "观察与提醒"),
            ("limits", "局限"),
            ("sources", "出处"),
        ]
        found = blocks(view)
        assert all(b["status"] == "done" for b in found.values())
        assert found["answer"]["content"] == NOTE["answer"]
        # 表取的是各步验过的交回物，不是成稿里重抄的
        assert found["metrics"]["content"] == METRICS["table"]
        assert found["metrics"]["from"] == {"step": 5, "title": "算指标"}
        assert found["metrics"]["artifact"]["name"] == "metrics"
        assert found["facts"]["content"] == FACTS["table"]
        assert found["facts"]["folded"] is True
        # AT-INV-26：数的出处——数据集、版本、数据时点
        assert found["metrics"]["source"] == {
            "dataset": PROFILE["dataset"],
            "data_version": METRICS["data_version"],
            "as_of": PROFILE.get("as_of"),
        }
        reconcile = found["reconcile"]["summary"]
        assert reconcile["rules"] == len(RECONCILE["checks"])
        assert reconcile["unbalanced"] == 0
        assert reconcile["text"].startswith(f"{len(RECONCILE['checks'])} 条规则 × ")
        assert "全部平" in reconcile["text"]
        cross = found["crosscheck"]["summary"]
        assert cross["matched"] == len(CROSSCHECK["matched"])
        assert cross["mismatched"] == 0
        assert cross["coverage"] == CROSSCHECK["coverage"]
        # 小结的块只给小结；明细是它后面的三张表，列名是给人看的
        assert found["crosscheck"]["content"] is None
        assert found["mismatched"]["content"] == []
        assert found["matched"]["content"] == CROSSCHECK["matched"]
        assert found["matched"]["folded"] is True
        assert {"key": "source_report", "title": "出自"} in found["matched"]["columns"]
        assert found["metrics"]["columns"][0] == {
            "key": "display_name",
            "title": "指标",
        }
        assert found["caveats"]["content"] == NOTE["caveats"]
        assert found["citations"]["content"] == NOTE["citations"]
        assert [h["status"] for h in view["how"]] == ["accepted"] * 7
        # AT-INV-27：结论栏是空的；任何地方没有评级、目标价、买卖建议的栏
        assert view["conclusion"] == {
            "text": "",
            "kind": "user_draft",
            "saved_at": None,
            "version": None,
        }
        shown = json.dumps(view, default=str, ensure_ascii=False)
        for word in ("rating", "target_price", "评级", "目标价", "method"):
            assert word not in shown, word
    finally:
        await close(runner)
        await fake.close()


async def test_the_conclusion_is_what_the_user_saved_and_nothing_else(db):
    fake, runner, *_, task_id = await run(db, ALL)
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                for words in ("先记一笔", "毛利率三年持平，我认为定价权还在"):
                    await repo.put_artifact(
                        task_id=task_id,
                        attempt_id=None,
                        name="conclusion",
                        kind="user_draft",
                        content={"text": words, "author": "user"},
                    )
        view = await dossier(db, task_id)
        assert view["conclusion"]["text"] == "毛利率三年持平，我认为定价权还在"
        assert view["conclusion"]["version"] == 2
        assert view["conclusion"]["kind"] == "user_draft"
        assert "conclusion" not in blocks(view)
    finally:
        await close(runner)
        await fake.close()


async def test_a_review_that_stopped_halfway(db):
    """AT-INV-28：停在第 3 步。前面的照常显示，后面的写「没有做到这一步」，费用如实。"""
    fake, runner, *_, task_id = await run(db, [SCOPE, PROFILE, unbalanced()])
    try:
        view = await dossier(db, task_id)
        assert view["head"] == {"kind": "running", "text": "专家还在做"}
        found = blocks(view)
        assert {k for k, b in found.items() if b["status"] == "done"} == set()
        for block in found.values():
            assert block["status"] == "not_reached"
            assert block["note"] == "没有做到这一步"
            assert block["content"] is None
        assert [h["status"] for h in view["how"]][:3] == [
            "accepted",
            "accepted",
            "waiting",
        ]
        assert view["task"]["budget"]["used"] == "0.03"
        assert view["task"]["data"]["dataset"] == PROFILE["dataset"]
    finally:
        await close(runner)
        await fake.close()


async def test_a_pack_without_its_own_layout_gets_the_common_one(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner, _, _, task_id = await seed(db, fake)
    try:
        await drive(runner)
        view = await dossier(db, task_id)
        assert [s["key"] for s in view["sections"]] == ["answer", "sources"]
        found = blocks(view)
        assert found["answer"]["content"] == json.loads(ANSWER)["answer"]
        assert found["citations"]["content"] == json.loads(ANSWER)["citations"]
    finally:
        await close(runner)
        await fake.close()


async def test_the_export(db):
    fake, runner, *_, task_id = await run(db, ALL)
    try:
        async with db() as s:
            text = await ExpertDesk(WorkbenchRepository(s)).dossier_export(
                task_id, owner_actor_id=OWNER
            )
        lines = text.splitlines()
        assert lines[0].startswith("# ")
        assert "财报体检 v1 · 已完成 · 花了 0.07 / 10.00 CNY（估算）" in lines
        heads = [line for line in lines if line.startswith("## ")]
        assert heads == [
            "## 一、回答",
            "## 二、数据",
            "## 三、专家验过什么",
            "## 四、观察与提醒",
            "## 五、局限",
            "## 六、出处",
            "## 七、它是怎么做的",
            "## 八、我的结论",
        ]
        assert NOTE["answer"] in text
        assert "| --- |" in text  # 表画成了表
        assert "| 编号 | 规则 | 验了几期 | 不平的期数 |" in lines  # 列名是给人看的
        assert "fiscal_year" not in text and "periods_checked" not in text
        assert lines[lines.index("## 一、回答") + 2] == NOTE["answer"]  # 节名不重复写
        assert "7. 成稿：做完，验收通过" in lines
        assert lines[-1] == "（还没有写）"
    finally:
        await close(runner)
        await fake.close()


async def test_only_the_owner_reads_it(db):
    fake, runner, *_, task_id = await run(db, ALL[:1])
    try:
        with pytest.raises(NotFound):
            await dossier(db, task_id, owner=str(uuid.uuid4()))
    finally:
        await close(runner)
        await fake.close()
