"""作为运行方跑分身测试（SDD 0027 §3.4）：真数据库、真 runner、假 app-server 按脚本回复。

受理 → 推进器在测试电脑的固定项目里开对话、用草稿快照建委托 → runner 驱动 → 收结果。
有人值守：开步前的提问没有预给答案就报成待回答的问题，knowlab 交来答案后代测试账号回答，接着跑。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from knowbot_fixtures import manifest
from sqlalchemy import text
from test_workbench_advisor_db import ANSWER, PLAN, ScriptedAppServer, close, drive
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.agent_service import AgentOffline
from app.application.workbench.deliverable_check import ReadFile
from app.application.workbench.test_runs import (
    KnowbotTestConductor,
    KnowbotTestConfig,
    KnowbotTestService,
)
from app.bootstrap.workbench import build_runner, workbench_stores
from app.domain.workbench.errors import (
    KnowbotInvalid,
    KnowbotTestBudgetExhausted,
    KnowbotTestConflict,
    KnowbotTestUnconfigured,
    NotFound,
    QuestionNotOpen,
)
from app.domain.workbench.test_runs import Answers, RunRequest
from app.infrastructure.workbench.publisher import RedisPublisher
from app.infrastructure.workbench.repository import WorkbenchRepository

WORKSPACE = "/workspace"
TESTER = "knowbot-tester"
CONFIG = KnowbotTestConfig(subject=TESTER, workspace_root=WORKSPACE)
ASK = [
    {
        "id": "period",
        "question": "看哪几年？",
        "options": [{"id": "3y", "label": "近三年"}],
    }
]


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.at


def request(run_id: str, *, doc=None, answers=None, expect="README", budget="10"):
    return RunRequest.model_validate(
        {
            "contract": "knowbot-test-run/v1",
            "run_id": run_id,
            "owner_subject": "user-123",
            "manifest": doc or manifest(),
            "cases": [
                {
                    "case_id": "c1",
                    "input": {
                        "question": "这个项目里有什么？",
                        "answers": answers or {},
                    },
                    "expectations": [
                        {
                            "artifact": "answer",
                            "path": "answer",
                            "kind": "contains",
                            "value": expect,
                        }
                    ],
                }
            ],
            "budget": {"limit": budget},
            "timeout_seconds": 3600,
            "max_wait_seconds": 600,
        }
    )


def asking() -> dict:
    doc = manifest()
    doc["pack"]["workflow"][0]["ask_before"] = ASK
    return doc


async def setup_test_computer(db, fake) -> None:
    """测试账号登录过、测试电脑已配对（登记了工作区根目录）、有自己的沙箱。"""
    async with db() as s:
        await s.execute(
            text(
                """insert into auth_user (id, issuer, subject, roles, scopes)
                   values (:i, 'https://casdoor.test', :s, '[]', '[]')"""
            ),
            {"i": OWNER, "s": TESTER},
        )
        await s.commit()
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await repo.register_environment(
                owner_actor_id=OWNER,
                name="knowbot-test",
                agent_version="0.4.1",
                codex_version="0.155.1",
                roots=[WORKSPACE],
                ceiling={},
            )
            await repo.register_sandbox(
                owner_actor_id=OWNER,
                app_server_url=f"ws://127.0.0.1:{fake.port}",
                token_ref="inline:t",
                codex_version="0.155.1",
            )


def runner_for(db, records=False):
    return build_runner(
        db,
        publisher=RedisPublisher(None, "t"),
        runner_id="r",
        poll_seconds=0.05,
        records=records,
    )


async def accept(db, req, config=CONFIG):
    async with db() as s:
        return await KnowbotTestService(WorkbenchRepository(s), config).accept(
            req, caller="knowlab"
        )


async def status(db, run_id):
    async with db() as s:
        return await KnowbotTestService(WorkbenchRepository(s), CONFIG).status(run_id)


async def answer(db, run_id, body):
    async with db() as s:
        return await KnowbotTestService(WorkbenchRepository(s), CONFIG).answer(
            run_id, Answers.model_validate({"contract": "knowbot-test-run/v1", **body})
        )


async def test_a_draft_runs_on_the_real_engine_and_passes(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        accepted = await accept(db, request("knowlab-run-0001"))
        assert accepted["status"] == "accepted" and accepted["cases"] == []
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()  # 开对话、建委托、排驾驶
        await drive(runner)
        await conductor.tick()  # 收这条用例的结果
        await conductor.tick()  # 全部用例都有结果：收尾
        done = await status(db, "knowlab-run-0001")
        assert done["status"] == "passed", done
        assert "progress" not in done  # 有结论就不带进度
        [case] = done["cases"]
        assert case["status"] == "passed"
        assert [s["step_id"] for s in case["steps"]] == ["plan", "answer"]
        assert all(s["accepted"] for s in case["steps"])
        assert case["expectations"] == [True]
        # 不回交回物的内容（C-L5）：模型写的回答原文不出现在结果里
        assert "The project has one README" not in json.dumps(done, ensure_ascii=False)
        assert Decimal(done["cost"]["spent"]) > 0
        async with db() as s:
            task = (
                await s.execute(
                    text(
                        "select pack_source, profile_id, owner_actor_id from workbench_tasks"
                    )
                )
            ).one()
        assert task.pack_source == "knowlab-test"
        assert task.profile_id.startswith("knowlab-test:")
        assert str(task.owner_actor_id) == OWNER  # 跑在测试账号上
    finally:
        await close(runner)
        await fake.close()


class FakeReader:
    """测试电脑上的代理：读成稿（SDD 0037）。"""

    def __init__(
        self, text: str | None = "# 成稿\n\n回答在这里", error=None, mtime_ms=None
    ) -> None:
        self.text = text
        self.error = error
        self.mtime_ms = mtime_ms  # None：代理没报修改时间
        self.calls: list[dict] = []

    async def read(self, *, owner, directory, path):
        self.calls.append({"owner": owner, "directory": directory, "path": path})
        if self.error is not None:
            raise self.error
        return None if self.text is None else ReadFile(self.text, self.mtime_ms)


async def mark_written(db) -> None:
    """代理认了成稿写到了（验收时记下 workspace_path）：脚本化的 app-server 不发 fileChange。"""
    async with db() as s:
        await s.execute(
            text(
                """update workbench_artifacts set workspace_path = '底稿/烟测-回答.md'
                   where name = 'answer'"""
            )
        )
        await s.commit()


async def outputs(db, run_id, case_id="c1"):
    async with db() as s:
        return await KnowbotTestService(WorkbenchRepository(s), CONFIG).outputs(
            run_id, case_id
        )


async def test_each_case_keeps_its_deliverable_and_step_outputs(db):
    """SDD 0037：用例记结果时取成稿（只取这次写了的）与各步交回物；结果本身照旧不带内容。"""
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    reader = FakeReader()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-out1"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, reader=reader)
        await conductor.tick()
        await drive(runner)
        await mark_written(db)
        await conductor.tick()
        await conductor.tick()
        done = await status(db, "knowlab-run-out1")
        assert done["status"] == "passed"
        assert "The project has one README" not in json.dumps(done, ensure_ascii=False)
        got = await outputs(db, "knowlab-run-out1")
        assert got["contract"] == "knowbot-test-run/v1" and got["case_id"] == "c1"
        assert got["deliverable"] == {
            "path": "底稿/烟测-回答.md",
            "text": "# 成稿\n\n回答在这里",
            "truncated": False,
            "step_id": "answer",  # 哪一步写的（审读 10-19）
        }
        assert got["deliverable_note"] is None and got["error"] is None
        [call] = reader.calls
        assert call["owner"] == OWNER and call["path"] == "底稿/烟测-回答.md"
        assert call["directory"] == WORKSPACE
        plan, answer_step = got["steps"]
        assert (plan["step_id"], plan["outcome"]) == ("plan", "accepted")
        assert plan["artifact"]["plan"] == ["read README", "answer"]
        assert answer_step["artifact"]["answer"] == "The project has one README."
        assert all(c["passed"] for c in answer_step["checks"])
        with pytest.raises(NotFound):
            await outputs(db, "knowlab-run-out1", "no-such-case")
    finally:
        await close(runner)
        await fake.close()


async def test_a_case_that_wrote_nothing_reads_nothing_and_a_failed_read_is_noted(db):
    """没记下写了成稿就不读（工作区共用，碰巧在那儿的可能是上一条的）；读失败照实写、结论不变。"""
    fake = ScriptedAppServer([PLAN, ANSWER, PLAN, ANSWER, PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        reader = FakeReader()
        await accept(db, request("knowlab-run-out2"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, reader=reader)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        got = await outputs(db, "knowlab-run-out2")
        assert reader.calls == [] and got["deliverable"] is None
        assert "没有写成稿" in got["deliverable_note"]
        assert [s["outcome"] for s in got["steps"]] == ["accepted", "accepted"]

        offline = FakeReader(error=AgentOffline("no agent online"))
        await accept(db, request("knowlab-run-out3"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, reader=offline)
        await conductor.tick()
        await drive(runner)
        async with db() as s:
            await s.execute(
                text(
                    """update workbench_artifacts set workspace_path = '底稿/烟测-回答.md'
                       where name = 'answer' and workspace_path is null"""
                )
            )
            await s.commit()
        await conductor.tick()
        await conductor.tick()
        assert (await status(db, "knowlab-run-out3"))["status"] == "passed"
        got = await outputs(db, "knowlab-run-out3")
        assert got["deliverable"] is None
        assert got["deliverable_note"] == "成稿没读到：agent_offline"
        unwired = await outputs(db, "knowlab-run-out2")
        assert unwired["case_id"] == "c1"

        # 文件的修改时间早于这条用例开始：不是这次写的，不认（审读 10-19 第 3 条）
        stale = FakeReader(mtime_ms=0)
        await accept(db, request("knowlab-run-out4"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, reader=stale)
        await conductor.tick()
        await drive(runner)
        async with db() as s:
            await s.execute(
                text(
                    """update workbench_artifacts set workspace_path = '底稿/烟测-回答.md'
                       where name = 'answer' and workspace_path is null"""
                )
            )
            await s.commit()
        await conductor.tick()
        await conductor.tick()
        got = await outputs(db, "knowlab-run-out4")
        assert got["deliverable"] is None
        assert got["deliverable_note"] == "成稿文件早于这条用例开始，不是这次写的"
    finally:
        await close(runner)
        await fake.close()


async def test_a_crash_while_judging_ends_the_case_as_an_error(db, monkeypatch):
    """判分时平台自己抛了异常（K19：算出 0 被当成没数）：委托记失败、这条用例记出错并收尾，
    不挂到超时。"""
    from decimal import InvalidOperation

    from app.application.workbench import advisor as advisor_module

    async def crash(*args, **kwargs):
        raise InvalidOperation

    monkeypatch.setattr(advisor_module, "judge_numbers", crash)
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-crash"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        done = await status(db, "knowlab-run-crash")
        assert done["status"] in ("failed", "error"), done
        [case] = done["cases"]
        assert case["status"] == "error"
        assert "plan: the platform failed during this step" in case["note"]
        async with db() as s:
            task = (
                await s.execute(
                    text("select state, active_attempt_id from workbench_tasks")
                )
            ).one()
            attempt = (
                await s.execute(
                    text("select status, failure_code from workbench_attempts")
                )
            ).one()
        assert task.state == "FAILED" and task.active_attempt_id is None
        assert (attempt.status, attempt.failure_code) == ("FAILED", "platform")
    finally:
        await close(runner)
        await fake.close()


async def test_unmet_expectations_fail_the_case(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0002", expect="不存在的说法"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        done = await status(db, "knowlab-run-0002")
        assert done["status"] == "failed"
        assert done["cases"][0]["status"] == "failed"
        assert done["cases"][0]["expectations"] == [False]
    finally:
        await close(runner)
        await fake.close()


async def test_a_question_the_expert_declines_fails_the_case_and_says_why(db):
    """受理关口（账 130）：测试里没人点「照做」，记失败，并写明专家认为问题不归它、理由是什么。"""
    decline = json.dumps(
        {"out_of_scope": True, "reason": "这是在问天气", "suggestion": "直接问"},
        ensure_ascii=False,
    )
    fake = ScriptedAppServer([decline])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0009"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        done = await status(db, "knowlab-run-0009")
        assert done["status"] == "failed"
        case = done["cases"][0]
        assert case["status"] == "failed"
        assert "out of its scope" in case["note"] and "这是在问天气" in case["note"]
    finally:
        await close(runner)
        await fake.close()


async def test_one_test_runs_at_a_time_and_the_next_waits_its_turn(db):
    """账 129：同时只跑一个测试；后受理的排队，不开对话、不计时，前一个结束才开始。"""
    fake = ScriptedAppServer([PLAN, ANSWER, PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0010"))
        await accept(db, request("knowlab-run-0011"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        async with db() as s:
            repo = WorkbenchRepository(s)
            first = await repo.get_test_run("knowlab-run-0010")
            second = await repo.get_test_run("knowlab-run-0011")
            assert first["started_at"] is not None
            assert second["started_at"] is None and second["status"] == "accepted"
            assert not any(
                c.get("task_id") for c in await repo.list_test_cases("knowlab-run-0011")
            )
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        assert (await status(db, "knowlab-run-0010"))["status"] == "passed"
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        assert (await status(db, "knowlab-run-0011"))["status"] == "passed"
    finally:
        await close(runner)
        await fake.close()


async def test_a_case_queued_behind_another_task_says_so_and_is_capped(db):
    """用例的委托排在测试账号别的委托后面（同一项目一次一位）：进度里带挡路的委托编号；排队不算进超时，
    但排到这次测试的时限还没轮到，按超时收、原因写清排在谁后面（0034 §三.3）。"""
    from dataclasses import replace

    fake = ScriptedAppServer([])
    await fake.start()
    clock = Clock()
    try:
        await setup_test_computer(db, fake)
        ahead = request("knowlab-run-0030").model_copy(
            update={"timeout_seconds": 86400}
        )
        await accept(db, ahead)
        await accept(db, request("knowlab-run-0031"))
        conductor = KnowbotTestConductor(
            workbench_stores(db), replace(CONFIG, max_running=2), now=clock
        )
        await conductor.tick()  # 两次都开跑；第二次的用例委托排在第一次的后面
        async with db() as s:
            [first] = await WorkbenchRepository(s).list_test_cases("knowlab-run-0030")
        await conductor.tick()  # 开始记排队时间
        shown = await status(db, "knowlab-run-0031")
        assert shown["status"] == "running"
        assert shown["progress"]["queued_behind"] == str(first["task_id"])
        clock.at += timedelta(seconds=1800)
        await conductor.tick()
        assert (await status(db, "knowlab-run-0031"))["status"] == "running"
        clock.at += timedelta(seconds=1801)  # 排队过了时限（3600 秒）
        await conductor.tick()
        done = await status(db, "knowlab-run-0031")
        assert done["status"] == "timeout"
        assert done["cases"][0]["status"] == "error"
        assert f"queued too long behind task {first['task_id']}" in json.dumps(done)
        assert (await status(db, "knowlab-run-0030"))["status"] == "running"
    finally:
        await fake.close()


class Folders:
    """测试电脑上的目录：记下建了哪个、删了哪个；ready=False 时建不出干净目录。"""

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[tuple[str, str]] = []

    async def prepare(self, *, owner, root, folder):
        assert root == WORKSPACE
        self.calls.append(("prepare", folder))
        return self.ready

    async def remove(self, *, owner, root, folder):
        self.calls.append(("remove", folder))


async def test_each_test_gets_its_own_folder_made_before_and_removed_after(db):
    """账 129：每次测试一个项目、一个目录；开跑前建好，测完删掉，下一个才开跑。"""
    fake = ScriptedAppServer([PLAN, ANSWER, PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    folders = Folders()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0012"))
        await accept(db, request("knowlab-run-0013"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, folders=folders)
        for _ in range(2):
            await conductor.tick()
            await drive(runner)
            await conductor.tick()
            await conductor.tick()
        assert (await status(db, "knowlab-run-0013"))["status"] == "passed"
        first, second = (c[1] for c in folders.calls if c[0] == "prepare")
        assert first.startswith("runs/") and first.endswith("-knowlab-run-0012")
        assert second.endswith("-knowlab-run-0013")
        assert folders.calls == [
            ("prepare", first),
            ("remove", first),
            ("prepare", second),
            ("remove", second),
        ]
        async with db() as s:
            repo = WorkbenchRepository(s)
            paths = set()
            for run_id in ("knowlab-run-0012", "knowlab-run-0013"):
                (case,) = await repo.list_test_cases(run_id)
                project = await repo.get_project(
                    str(case["project_id"]), owner_actor_id=OWNER
                )
                paths.add(project["path"])
            assert paths == {first, second}
    finally:
        await close(runner)
        await fake.close()


async def test_without_isolation_the_test_runs_in_the_workspace_and_says_so(db):
    """测试电脑的代理还不支持建目录（0.5.0 服务流只读）：隔离没打开，就在工作区本身里跑，
    每条用例写明没有隔离（账 129，所有者 2026-10-18 选甲）。"""
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0015"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        assert (await status(db, "knowlab-run-0015"))["status"] == "passed"
        async with db() as s:
            repo = WorkbenchRepository(s)
            (case,) = await repo.list_test_cases("knowlab-run-0015")
            project = await repo.get_project(
                str(case["project_id"]), owner_actor_id=OWNER
            )
            assert project["path"] == ""
            assert "没有隔离" in case["result"]["note"]
    finally:
        await close(runner)
        await fake.close()


async def test_no_clean_folder_means_no_start_and_then_an_error(db):
    """测试电脑给不出干净的目录：不开跑；过了这次测试的时限还不行，报错结束。"""
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    clock = Clock()
    folders = Folders(ready=False)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0014"))
        conductor = KnowbotTestConductor(
            workbench_stores(db), CONFIG, folders=folders, now=clock
        )
        await conductor.tick()
        async with db() as s:
            run = await WorkbenchRepository(s).get_test_run("knowlab-run-0014")
            assert run["started_at"] is None and run["status"] == "accepted"
        # 账 141：结果里写明在等什么
        got = await status(db, "knowlab-run-0014")
        assert got["status"] == "accepted" and got["blocked"] == "folder_not_ready"
        clock.at += timedelta(seconds=3601)
        await conductor.tick()
        assert (await status(db, "knowlab-run-0014"))["status"] == "error"
        async with db() as s:
            run = await WorkbenchRepository(s).get_test_run("knowlab-run-0014")
            assert "clean folder" in run["error"]
    finally:
        await fake.close()


async def test_an_offline_test_computer_never_starts_and_says_who_is_online(db, caplog):
    """账 133：测试电脑不在线不开跑；测试账号下在线的是别的电脑时，报错里写出来。"""
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    clock = Clock()
    try:
        await setup_test_computer(db, fake)
        async with db() as s:
            await s.execute(
                text(
                    "update workbench_environments set status = 'offline' where name = 'knowbot-test'"
                )
            )
            await s.commit()
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.register_environment(
                    owner_actor_id=OWNER,
                    name="zymun",
                    agent_version="0.5.0",
                    codex_version="0.155.1",
                    roots=["C:\\Users\\zymun\\Desktop"],
                    ceiling={},
                )
        await accept(db, request("knowlab-run-0015"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, now=clock)
        with caplog.at_level(
            logging.INFO, logger="app.application.workbench.test_runs"
        ):
            await conductor.tick()
            clock.at += timedelta(seconds=30)
            await conductor.tick()  # 同一原因不到 10 分钟：不再记
            clock.at += timedelta(minutes=10)
            await conductor.tick()  # 满 10 分钟：再记一行
        waiting = [
            r.getMessage()
            for r in caplog.records
            if "knowbot test waiting" in r.getMessage()
        ]
        assert len(waiting) == 2 and all(
            "run=knowlab-run-0015 reason=computer_offline" in m for m in waiting
        )
        assert (await status(db, "knowlab-run-0015"))["blocked"] == "computer_offline"
        async with db() as s:
            repo = WorkbenchRepository(s)
            assert (await repo.get_test_run("knowlab-run-0015"))["started_at"] is None
            assert not any(
                c.get("task_id") for c in await repo.list_test_cases("knowlab-run-0015")
            )
        clock.at += timedelta(seconds=3601)
        await conductor.tick()
        async with db() as s:
            run = await WorkbenchRepository(s).get_test_run("knowlab-run-0015")
            assert run["status"] == "error"
            assert "offline" in run["error"] and "zymun" in run["error"]
    finally:
        await fake.close()


async def test_an_unanswered_ask_before_waits_for_knowlab_then_continues(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0003", doc=asking()))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        waiting = await status(db, "knowlab-run-0003")
        assert waiting["status"] == "waiting", waiting
        # 没结论时带进度（待办账 135）：第 1 条、第 1 步第 1 次（开步前就停下问了），没花钱
        progress = waiting["progress"]
        assert {k: progress[k] for k in ("case_id", "cases_done", "cases_total")} == {
            "case_id": "c1",
            "cases_done": 0,
            "cases_total": 1,
        }
        assert (progress["step"], progress["steps_total"], progress["attempt"]) == (
            1,
            2,
            1,
        )
        assert progress["step_title"] and progress["spent"] == "0.000000"
        [q] = waiting["questions"]
        assert q["kind"] == "ask_before" and q["key"] == "period"
        assert q["case_id"] == "c1" and q["options"] == [
            {"id": "3y", "label": "近三年"}
        ]
        with pytest.raises(QuestionNotOpen):
            await answer(
                db,
                "knowlab-run-0003",
                {
                    "answers": [
                        {"question_id": "nope", "text": "x", "answered_by": "user"}
                    ]
                },
            )
        body = {
            "answers": [
                {
                    "question_id": q["question_id"],
                    "option_id": "3y",
                    "answered_by": "user",
                }
            ]
        }
        await answer(db, "knowlab-run-0003", body)
        await answer(db, "knowlab-run-0003", body)  # 同样的答案重复交：原样
        with pytest.raises(KnowbotTestConflict):
            await answer(
                db,
                "knowlab-run-0003",
                {
                    "answers": [
                        {
                            "question_id": q["question_id"],
                            "text": "近五年",
                            "answered_by": "user",
                        }
                    ]
                },
            )
        await conductor.tick()  # 代测试账号回答，委托回到排队
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        done = await status(db, "knowlab-run-0003")
        assert done["status"] == "passed", done
        assert any("近三年" in t for t in fake.turn_inputs)  # 答案进了那一步的输入
    finally:
        await close(runner)
        await fake.close()


async def test_preset_answers_are_used_without_asking(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(
            db, request("knowlab-run-0004", doc=asking(), answers={"period": "3y"})
        )
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        assert (await status(db, "knowlab-run-0004"))["questions"] == []
        await conductor.tick()  # 用用例里预给的答案代答
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        assert (await status(db, "knowlab-run-0004"))["status"] == "passed"
        assert any("近三年" in t for t in fake.turn_inputs)
    finally:
        await close(runner)
        await fake.close()


async def test_a_question_nobody_answers_ends_as_needs_human(db):
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    clock = Clock()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0005", doc=asking()))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, now=clock)
        await conductor.tick()
        await drive(runner)
        await conductor.tick()  # 开始计等待
        clock.at += timedelta(seconds=700)  # 超过最长等待（600 秒）
        await conductor.tick()
        await conductor.tick()
        done = await status(db, "knowlab-run-0005")
        assert done["status"] == "failed"
        assert done["cases"][0]["status"] == "needs_human"
        assert done["waited_seconds"] >= 700  # 等人的时间单独记，不算进超时
    finally:
        await close(runner)
        await fake.close()


async def test_acceptance_refusals_and_conflicts(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        with pytest.raises(KnowbotTestUnconfigured):
            await accept(db, request("knowlab-run-0010"), KnowbotTestConfig(None, None))
        await setup_test_computer(db, fake)
        bad = manifest()
        bad["pack"]["workflow"][0]["tools"] = ["run_sql"]  # 数据线工具（C-L2）
        with pytest.raises(KnowbotInvalid):
            await accept(db, request("knowlab-run-0011", doc=bad))
        await accept(db, request("knowlab-run-0012"))
        again = await accept(db, request("knowlab-run-0012"))
        assert again["run_id"] == "knowlab-run-0012"
        with pytest.raises(KnowbotTestConflict):
            await accept(db, request("knowlab-run-0012", budget="11"))
        # 每月上限 1000：在跑的按上限占用
        await accept(db, request("knowlab-run-0013", budget="980"))
        with pytest.raises(KnowbotTestBudgetExhausted):
            await accept(db, request("knowlab-run-0014", budget="20"))
    finally:
        await fake.close()


async def test_timeouts_and_seven_day_cleanup(db):
    fake = ScriptedAppServer([PLAN])  # 第二步一直没有合格回复
    await fake.start()
    runner = runner_for(db)
    clock = Clock()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0020"))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG, now=clock)
        await conductor.tick()
        clock.at += timedelta(seconds=3601)
        await conductor.tick()
        done = await status(db, "knowlab-run-0020")
        assert done["status"] == "timeout"
        assert done["cases"][0]["status"] == "error"
        # 超时的也留下做到哪（SDD 0037）：这里 runner 没开动，两步都没走到
        kept = await outputs(db, "knowlab-run-0020")
        assert [x["outcome"] for x in kept["steps"]] == ["not_reached", "not_reached"]
        assert "没有写成稿" in kept["deliverable_note"]
        async with db() as s:
            sessions = (
                await s.execute(text("select count(*) from workbench_sessions"))
            ).scalar_one()
        assert sessions == 1
        clock.at += timedelta(days=7, seconds=1)
        assert await conductor.cleanup() == 1
        async with db() as s:
            left = (
                await s.execute(text("select count(*) from workbench_sessions"))
            ).scalar_one()
            tasks = (
                await s.execute(text("select count(*) from workbench_tasks"))
            ).scalar_one()
        assert (left, tasks) == (0, 0)
        assert (await status(db, "knowlab-run-0020"))["status"] == "timeout"  # 结果留着
        assert (await outputs(db, "knowlab-run-0020"))["steps"]  # 产出 30 天内还在
        clock.at += timedelta(days=23)
        await conductor.cleanup()
        with pytest.raises(NotFound):
            await outputs(db, "knowlab-run-0020")
        assert (await status(db, "knowlab-run-0020"))["status"] == "timeout"
    finally:
        await close(runner)
        await fake.close()


@pytest.fixture
def unique_run():
    return f"knowlab-run-{uuid.uuid4().hex[:8]}"


async def test_a_mid_step_approval_is_answered_through_knowlab(db):
    """中途的工具审批（runner 级的等待）：报成 tool_approval 问题，运营批准后答复送到 app-server。"""
    import asyncio

    from test_workbench_runner_db import FakeAppServer, wait_for

    fake = FakeAppServer()
    await fake.start()
    runner = runner_for(db)
    running = asyncio.create_task(runner.run_forever())
    try:
        await setup_test_computer(db, fake)
        req = request("knowlab-run-0030")
        body = req.model_dump(mode="json")
        body["cases"][0]["input"]["question"] = "NEED_APPROVAL 看看项目"
        await accept(db, RunRequest.model_validate(body))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)

        async def asked():
            await conductor.tick()
            current = await status(db, "knowlab-run-0030")
            return current["status"] == "waiting"

        assert await wait_for(asked, timeout=15)
        [q] = (await status(db, "knowlab-run-0030"))["questions"]
        assert q["kind"] == "tool_approval" and q["allow_free_text"] is False
        assert "touch /home/u/x" in q["text"]
        await answer(
            db,
            "knowlab-run-0030",
            {
                "answers": [
                    {
                        "question_id": q["question_id"],
                        "decision": "approve",
                        "answered_by": "operator",
                    }
                ]
            },
        )

        async def approved():
            await conductor.tick()
            return any(
                (d.get("result") or {}).get("decision") == "accept"
                for d in fake.approval_decisions
            )

        assert await wait_for(approved, timeout=15)
        async with db() as s:
            rows = (
                await s.execute(
                    text("select decision, source from workbench_approval_log")
                )
            ).all()
        assert ("accept", "user") in [tuple(r) for r in rows]  # 代测试账号批准的
    finally:
        runner.stop()
        await asyncio.wait_for(running, 10)
        # 还在驾驶的那一步握着数据库连接：停掉它，免得夹具收尾删 schema 时等锁
        drivers = list(runner.driving.values())
        for task in drivers:
            task.cancel()
        await asyncio.gather(*drivers, return_exceptions=True)
        await close(runner)
        await fake.close()


async def test_an_answer_that_cannot_go_in_is_noted_once_not_every_tick(db, caplog):
    """预给的答复答不进去（问题已失效）：记一次原因，测试不每 5 秒整轮报错一遍（2026-10-19 K19 第 2 稿）。"""
    import asyncio
    import logging

    from test_workbench_runner_db import FakeAppServer, wait_for

    fake = FakeAppServer()
    await fake.start()
    runner = runner_for(db)
    running = asyncio.create_task(runner.run_forever())
    try:
        await setup_test_computer(db, fake)
        req = request("knowlab-run-0040")
        body = req.model_dump(mode="json")
        body["cases"][0]["input"]["question"] = "NEED_APPROVAL 看看项目"
        await accept(db, RunRequest.model_validate(body))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)

        async def asked():
            await conductor.tick()
            return (await status(db, "knowlab-run-0040"))["status"] == "waiting"

        assert await wait_for(asked, timeout=15)
        [q] = (await status(db, "knowlab-run-0040"))["questions"]
        async with db() as s:  # 问题打开之后委托又被动过：版本对不上，答不进去
            await s.execute(
                text(
                    "update workbench_tasks set state_version = state_version + 2 "
                    "where active_interaction_id is not null"
                )
            )
            await s.commit()
        await answer(
            db,
            "knowlab-run-0040",
            {
                "answers": [
                    {
                        "question_id": q["question_id"],
                        "decision": "approve",
                        "answered_by": "operator",
                    }
                ]
            },
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                await conductor.tick()
        assert not [r for r in caplog.records if "advance failed" in r.getMessage()]
        noted = [r for r in caplog.records if "could not answer" in r.getMessage()]
        assert len(noted) == 1
    finally:
        runner.stop()
        await asyncio.wait_for(running, 10)
        drivers = list(runner.driving.values())
        for task in drivers:
            task.cancel()
        await asyncio.gather(*drivers, return_exceptions=True)
        await close(runner)
        await fake.close()


class FakeDataSource:
    """假的 knowlab：记下用途，按清单的绑定回数据（0027 §3.5）。"""

    def __init__(self, binding: dict) -> None:
        self.binding = binding
        self.purposes: list[dict] = []
        self.sha = binding["sha256"]

    async def list(self, purpose):
        self.purposes.append(purpose)
        return [{**self.binding, "name": "年报.csv", "sha256": self.sha}]

    async def read(self, purpose, dataset_id, page):
        self.purposes.append(purpose)
        citation = {**self.binding, "sha256": self.sha, "page": page, "pages": 1}
        return {"dataset_id": dataset_id, "text": "营收,100", "citation": citation}

    async def cells(self, purpose, cells):
        """每一格都是 100（0030 §3.1）。"""
        self.purposes.append(purpose)
        return [
            {
                **ref,
                "found": True,
                "raw": "100",
                "number": "100",
                "version": "1",
                "sha256": self.sha,
            }
            for ref in cells
        ]


async def test_a_test_reads_its_bound_data_only_while_it_runs(db):
    """测试中的草稿经工作台读用户数据：委托在跑才转给 knowlab，结束了就拒（0027 §3.5）。"""
    from app.application.workbench.knowbot_data import KnowbotData
    from app.application.workbench.records import ProjectRecords
    from app.domain.workbench.errors import NotFound, RecordsRefused

    binding = {"dataset_id": str(uuid.uuid4()), "version": "1", "sha256": "a" * 64}
    doc = asking()
    doc["data_bindings"] = [binding]
    for step in doc["pack"]["workflow"]:
        step["tools"] = ["list_my_data", "read_my_data"]
    source = FakeDataSource(binding)
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db, records=True)  # 沙箱里配了工作台的工具服务
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0040", doc=doc))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        assert (await status(db, "knowlab-run-0040"))["status"] == "waiting"
        async with db() as s:
            task_id = str(
                (await s.execute(text("select id from workbench_tasks"))).scalar()
            )

        def data(s, owner=OWNER):
            return KnowbotData(
                ProjectRecords(WorkbenchRepository(s), owner_actor_id=owner), source
            )

        async with db() as s:
            listed = await data(s).list(task_id)
            assert [d["dataset_id"] for d in listed["datasets"]] == [
                binding["dataset_id"]
            ]
            read = await data(s).read(task_id, binding["dataset_id"])
            assert read["text"] == "营收,100"
            with pytest.raises(NotFound):
                await data(s).read(
                    task_id, str(uuid.uuid4())
                )  # 没绑定的：不去问 knowlab
            await s.commit()
        assert source.purposes == [{"kind": "test", "run_id": "knowlab-run-0040"}] * 2
        source.sha = "b" * 64  # knowlab 回来的不是绑定的那一份：不给
        async with db() as s:
            assert (await data(s).list(task_id))["datasets"] == []
            with pytest.raises(NotFound):
                await data(s).read(task_id, binding["dataset_id"])
        source.sha = binding["sha256"]
        async with db() as s:
            with pytest.raises(NotFound):  # 别人的委托
                await data(s, owner=str(uuid.uuid4())).list(task_id)
        [q] = (await status(db, "knowlab-run-0040"))["questions"]
        await answer(
            db,
            "knowlab-run-0040",
            {
                "answers": [
                    {
                        "question_id": q["question_id"],
                        "option_id": "3y",
                        "answered_by": "user",
                    }
                ]
            },
        )
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        assert (await status(db, "knowlab-run-0040"))["status"] == "passed"
        assert any(
            "## The user's own data" in t and task_id in t for t in fake.turn_inputs
        )
        async with db() as s:
            with pytest.raises(RecordsRefused):  # 委托结束了：不再转发
                await data(s).list(task_id)
    finally:
        await close(runner)
        await fake.close()


async def test_computed_numbers_are_recorded_and_checked_on_the_real_store(db):
    """0030：测试中的草稿用 compute 算数，记进计算记录；交回的数对不上就退回重做，对上了才过。"""
    from app.application.workbench.knowbot_compute import COMPUTE_EVENT, KnowbotCompute
    from app.application.workbench.records import ProjectRecords
    from app.domain.workbench import compute as calc

    binding = {"dataset_id": str(uuid.uuid4()), "version": "1", "sha256": "a" * 64}
    doc = asking()
    doc["data_bindings"] = [binding]
    for step in doc["pack"]["workflow"]:
        step["tools"] = ["list_my_data", "read_my_table", "compute"]
    last = doc["pack"]["workflow"][-1]
    last["acceptance"] = [
        *last["acceptance"],
        {
            "kind": "computed",
            "path": "figures",
            "expression": "a + b",
            "message": "数要由 compute 算",
            "label": "算出来的数",
        },
    ]
    last["max_reworks"] = 1
    source = FakeDataSource(binding)
    fake = ScriptedAppServer([])
    await fake.start()
    runner = runner_for(db, records=True)
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-run-0050", doc=doc))
        conductor = KnowbotTestConductor(workbench_stores(db), CONFIG)
        await conductor.tick()
        await drive(runner)
        async with db() as s:
            task_id = str(
                (await s.execute(text("select id from workbench_tasks"))).scalar()
            )
        cell = calc.address(binding["dataset_id"], "利润表", "营业收入", "2024-06-30")
        async with db() as s:
            tool = KnowbotCompute(
                ProjectRecords(WorkbenchRepository(s), owner_actor_id=OWNER), source
            )
            done = await tool.compute(task_id, "合计", "a + b", {"a": cell, "b": cell})
            await s.commit()
        assert done["value"] == "200"
        async with db() as s:
            events = await WorkbenchRepository(s).list_task_events(
                task_id, types=(COMPUTE_EVENT,)
            )
        assert [f"c{e['cursor']}" for e in events] == [done["id"]]
        assert events[0]["payload"]["inputs"][0]["ref"] == cell

        def answer_with(value):
            return json.dumps(
                {
                    **json.loads(ANSWER),
                    "figures": [{"compute_id": done["id"], "value": value}],
                }
            )

        fake.replies += [PLAN, answer_with(201), answer_with("200")]
        [q] = (await status(db, "knowlab-run-0050"))["questions"]
        await answer(
            db,
            "knowlab-run-0050",
            {
                "answers": [
                    {
                        "question_id": q["question_id"],
                        "option_id": "3y",
                        "answered_by": "user",
                    }
                ]
            },
        )
        await conductor.tick()
        await drive(runner)
        await conductor.tick()
        await conductor.tick()
        assert (await status(db, "knowlab-run-0050"))["status"] == "passed"
        async with db() as s:
            judged = await WorkbenchRepository(s).list_task_events(
                task_id, types=("step/rejected", "step/accepted")
            )
        computed = [
            (e["type"], c["pass"])
            for e in judged
            for c in e["payload"]["checks"]
            if c["rule"] == "computed"
        ]
        assert computed == [("step/rejected", False), ("step/accepted", True)]
        assert any("read_my_table" in t and "compute_id" in t for t in fake.turn_inputs)
        assert any(
            "says 201 but computed 200" in t for t in fake.turn_inputs
        )  # 重做提示里有
        [rejected] = [e for e in judged if e["type"] == "step/rejected"]
        assert any(
            "says 201 but computed 200" in c["detail"]
            for c in rejected["payload"]["checks"]
        )
    finally:
        await close(runner)
        await fake.close()


async def test_a_test_with_a_data_grant_reads_what_is_pinned(db):
    """SDD 0042 第七节：带测试运行授权的测试，每条用例的委托钉授权里那几份，按钉住的读；
    授权由 knowlab 撤，这里不交还。"""
    import hashlib

    from app.application.workbench.data_choice import GrantReleaser
    from app.application.workbench.knowbot_data import data_scope

    csv = "项目,2024\n营业收入,250.25\n".encode()
    dataset_id = str(uuid.uuid4())
    sha = hashlib.sha256(csv).hexdigest()
    binding = {"dataset_id": dataset_id, "version": "1", "sha256": sha}
    doc = asking()
    doc["data_bindings"] = [binding]
    for step in doc["pack"]["workflow"]:
        step["tools"] = ["list_my_data", "read_my_data"]
    body = request("knowlab-run-0060", doc=doc).model_dump(mode="json")
    body["data_grant"] = {
        "grant_id": "grant-test-1",
        "items": [
            {
                "dataset_id": dataset_id,
                "item_id": "item-1",
                "name": "营收.csv",
                "media_type": "text/csv; charset=utf-8",
                "size_bytes": len(csv),
                "sha256": sha,
            }
        ],
    }
    fake = ScriptedAppServer([PLAN, ANSWER])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        await accept(db, RunRequest.model_validate(body))
        await KnowbotTestConductor(workbench_stores(db), CONFIG).tick()
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = (
                (await s.execute(text("select * from workbench_tasks")))
                .mappings()
                .one()
            )
            # 这段对话是测试的一条用例：沙箱不给写 /tmp（Fable 评审 P2-14）
            assert await repo.is_test_session(str(task["session_id"]))
            assert not await repo.is_test_session(str(uuid.uuid4()))
            pins = await repo.list_task_data(str(task["id"]))
            assert [
                (str(p["dataset_id"]), p["grant_id"], p["grant_kind"]) for p in pins
            ] == [(dataset_id, "grant-test-1", "test")]
            purpose, bound = await data_scope(repo, OWNER, dict(task))
            assert purpose["kind"] == "pinned" and dataset_id in bound
            await s.execute(
                text("update workbench_tasks set state = 'SUCCEEDED' where id = :t"),
                {"t": task["id"]},
            )
            await s.commit()

        class Cloud:
            released: list[str] = []

            async def release(self, grant_id: str) -> None:
                self.released.append(grant_id)

        cloud = Cloud()
        assert await GrantReleaser(workbench_stores(db), cloud).sweep() == 0  # type: ignore[arg-type]
        assert cloud.released == []
    finally:
        await close(runner)
        await fake.close()


def test_a_grant_with_a_dataset_id_that_is_not_knowlabs_is_refused():
    """授权里的数据编号不是 knowlab 的：不受理，不退回经 knowlab 读（Astra 评审 F02）。"""
    from app.application.workbench.test_runs import pins_for_test
    from app.domain.workbench.errors import KnowbotInvalid

    body = request("knowlab-run-0062").model_dump(mode="json")
    body["data_grant"] = {
        "grant_id": "g",
        "items": [
            {
                "dataset_id": "not-a-uuid",
                "item_id": "i",
                "name": "a.csv",
                "media_type": "text/csv",
                "size_bytes": 1,
                "sha256": "a" * 64,
            }
        ],
    }
    with pytest.raises(KnowbotInvalid):
        pins_for_test(RunRequest.model_validate(body))


def test_a_request_without_a_grant_keeps_its_digest():
    plain = request("knowlab-run-0061")
    assert "data_grant" not in plain.model_dump(mode="json")
    assert (
        plain.digest()
        == RunRequest.model_validate(plain.model_dump(mode="json")).digest()
    )
