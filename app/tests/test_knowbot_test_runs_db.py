"""作为运行方跑分身测试（SDD 0027 §3.4）：真数据库、真 runner、假 app-server 按脚本回复。

受理 → 推进器在测试电脑的固定项目里开对话、用草稿快照建委托 → runner 驱动 → 收结果。
有人值守：开步前的提问没有预给答案就报成待回答的问题，knowlab 交来答案后代测试账号回答，接着跑。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from knowbot_fixtures import manifest
from sqlalchemy import text
from test_workbench_advisor_db import ANSWER, PLAN, ScriptedAppServer, close, drive
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

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
        clock.at += timedelta(seconds=3601)
        await conductor.tick()
        assert (await status(db, "knowlab-run-0014"))["status"] == "error"
        async with db() as s:
            run = await WorkbenchRepository(s).get_test_run("knowlab-run-0014")
            assert "clean folder" in run["error"]
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
