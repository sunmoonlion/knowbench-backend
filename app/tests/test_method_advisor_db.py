"""Existing Advisor runs frozen methods; real DB, scripted sandbox turn port."""

import asyncio
import json
from decimal import Decimal

import pytest
from method_plan_fixtures import catalog, proposal
from sqlalchemy import text
from test_execution_plan_db import open_step, prepared
from test_method_plans_db import task
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.acceptance import judge
from app.application.workbench.advisor import Advisor, StepAssessment
from app.application.workbench.execution_plan import execution_for_task
from app.application.workbench.ledger import Ledger
from app.application.workbench.method_planner import MethodPlanner
from app.application.workbench.plan_receipts import read_plan_version
from app.application.workbench.turn import TurnResult
from app.domain.workbench.method_plan import PLAN_STEP, output_name
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.states import AttemptState, TaskState, WaitingReason
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

pytestmark = pytest.mark.asyncio
ROWS = '{"rows": [{"id":"one"}]}'
OTHER_ROWS = '{"rows": [{"id":"two"}]}'


class Driver:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.results = []

    async def run_turn(self, thread_id, prompt, *, timeout):  # noqa: ASYNC109
        self.prompts.append(prompt)
        assert self.replies, "unexpected extra model turn"
        result = TurnResult(
            f"turn-{len(self.prompts)}", self.replies.pop(0), {}, cost=Decimal("0.01")
        )
        self.results.append(result)
        return result


async def patch_catalog(db, tid, change):
    document = catalog().model_dump(mode="json")
    change(document)
    async with db() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE workbench_tasks SET pack_snapshot=cast(:p as jsonb) WHERE id=:id"
                ),
                {"p": json.dumps(document), "id": tid},
            )


async def test_existing_advisor_plans_executes_and_delivers_all_final_outputs(db):
    tid, _ = await task(db)
    await patch_catalog(
        db, tid, lambda d: d["planning"].update(final_units=["inspect", "report"])
    )
    plan = proposal().model_dump(mode="json")
    plan["final_outputs"]["inspect"] = "inspect_1"
    driver = Driver([json.dumps(plan), ROWS, ROWS])
    assert (
        await Advisor(SqlWorkbenchStores(db), driver).drive(tid) == TaskState.SUCCEEDED
    )
    assert len(driver.prompts) == 3
    assert "## First: is this question for this expert?" in driver.prompts[1]
    assert output_name("inspect_1") in driver.prompts[2]
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["current_step"] == 2 and row["accepted_plan_version"] == 1
        assert Decimal(row["budget"]["used"]) == Decimal("0.03")
        assert Decimal(row["budget"]["reserved"]) == 0
        hb = await repo.get_artifact(task_id=tid, name="handback")
        assert str(row["terminal_result_ref"]) == str(hb["id"])
        assert set(hb["content"]["final_outputs"]) == {"inspect", "report"}
        attempts = await repo.list_attempts(tid)
        assert [a["step_id"] for a in attempts] == [PLAN_STEP, "inspect_1", "report_1"]
        assert all(a["status"] == AttemptState.COMPLETED for a in attempts)


@pytest.mark.parametrize("records", [True, False])
async def test_dynamic_data_planning_uses_selected_methods_and_cannot_skip_missing_mcp(
    db, records
):
    tid, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.insert_task_resolution(
                {
                    "task_id": tid,
                    "resolution_id": "synthetic-data",
                    "revision": 1,
                    "consumer": "expert:synthetic@1",
                    "grant_ref": "synthetic-task-grant",
                    "params": {},
                    "status": "planning",
                }
            )
    replies = [proposal().model_dump_json()]
    if records:
        replies += ["No additional data is needed for this synthetic task.", ROWS, ROWS]
    driver = Driver(replies)
    state = await Advisor(SqlWorkbenchStores(db), driver, records=records).drive(tid)
    assert state == (TaskState.SUCCEEDED if records else TaskState.FAILED)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] == 1
        resolution = await repo.get_task_resolution(tid)
        assert resolution["status"] == ("ready" if records else "planning")
        attempts = await repo.list_attempts(tid)
        assert [a["step_id"] for a in attempts] == (
            [PLAN_STEP, "data-plan", "inspect_1", "report_1"]
            if records
            else [PLAN_STEP]
        )
    if records:
        chosen = {u.contract.step_id: u.contract.title for u in catalog().method_units}
        assert (
            chosen["inspect"] in driver.prompts[1]
            and chosen["report"] in driver.prompts[1]
        )
        assert chosen["review"] not in driver.prompts[1]
        assert "First: is this question for this expert?" in driver.prompts[2]


async def test_backtracking_binds_new_inputs_and_excludes_old_outputs(db):
    tid, _ = await task(db)
    driver = Driver(
        [proposal().model_dump_json(), ROWS, "{}", "{}", OTHER_ROWS, OTHER_ROWS]
    )
    assert (
        await Advisor(SqlWorkbenchStores(db), driver).drive(tid) == TaskState.SUCCEEDED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        hb = await repo.get_artifact(task_id=tid, name="handback")
        verified = {a["artifact"]: a["version"] for a in hb["content"]["verified"]}
        assert verified == {output_name("inspect_1"): 2, output_name("report_1"): 1}
        attempts = await repo.list_attempts(tid)
        assert attempts[-1]["input_artifact_versions"][0]["version"] == 2
        assert Decimal((await repo.get_task(tid))["budget"]["used"]) == Decimal("0.06")


@pytest.mark.parametrize(
    "paths", [("analysis/report.md", "forecast/report.md"), (None, "reports/交回.md")]
)
async def test_colliding_placed_files_never_dispatch_methods_or_deliver(db, paths):
    tid, _ = await task(db)

    def with_files(document):
        document["planning"]["max_replans"] = 0
        for method_id, path in zip(("inspect", "report"), paths, strict=True):
            next(
                u
                for u in document["method_units"]
                if u["contract"]["step_id"] == method_id
            )["contract"]["output_file"] = path

    await patch_catalog(db, tid, with_files)
    driver = Driver([proposal().model_dump_json()])
    assert await Advisor(SqlWorkbenchStores(db), driver).drive(tid) == TaskState.FAILED
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["accepted_plan_version"] is None and row["current_step"] == 0
        assert Decimal(row["budget"]["used"]) == Decimal("0.01")
        assert Decimal(row["budget"]["reserved"]) == 0
        assert not await repo.list_artifacts(tid)
        attempts = await repo.list_attempts(tid)
        assert [a["step_id"] for a in attempts] == [PLAN_STEP]
        assert attempts[0]["failure_code"] == "method_plan_invalid"
        rejected = await repo.list_task_events(tid, types=("task/plan_rejected",))
        assert "conflicting output" in rejected[0]["payload"]["reason"]
    assert len(driver.prompts) == 1


async def test_restart_continues_accepted_plan_and_ignores_unaccepted_latest(db):
    tid, _ = await task(db)
    first = Advisor(
        SqlWorkbenchStores(db), Driver([proposal().model_dump_json(), ROWS])
    )
    assert await first._step_once(tid) == TaskState.QUEUED
    assert await first._step_once(tid) == TaskState.QUEUED
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=None,
                name=output_name("inspect_1"),
                kind="step",
                content=json.loads(OTHER_ROWS),
            )
    second = Driver([ROWS])
    assert (
        await Advisor(SqlWorkbenchStores(db), second).drive(tid) == TaskState.SUCCEEDED
    )
    assert len(second.prompts) == 1 and '"id": "one"' in second.prompts[0]
    async with db() as session:
        attempts = await WorkbenchRepository(session).list_attempts(tid)
        assert len([a for a in attempts if a["step_id"] == PLAN_STEP]) == 1
        assert attempts[-1]["input_artifact_versions"][0]["version"] == 1


async def test_cancel_during_remote_validation_is_not_blocked_and_accepts_no_result(db):
    tid, _ = await task(db)

    def with_file(d):
        next(u for u in d["method_units"] if u["contract"]["step_id"] == "inspect")[
            "contract"
        ]["output_file"] = "inspection.md"

    await patch_catalog(db, tid, with_file)

    class Files:
        async def exists(self, **kwargs):
            async with db() as session:
                await asyncio.wait_for(
                    Ledger(WorkbenchRepository(session)).request_cancel(
                        tid, owner_actor_id=OWNER
                    ),
                    timeout=3,
                )
            return True

    driver = Driver([proposal().model_dump_json(), ROWS])
    assert (
        await Advisor(SqlWorkbenchStores(db), driver, deliverables=Files()).drive(tid)
        == TaskState.CANCELLED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["current_step"] == 0 and Decimal(row["budget"]["used"]) == Decimal(
            "0.02"
        )
        assert not await repo.get_artifact(task_id=tid, name=output_name("inspect_1"))
        assert not await repo.get_artifact(task_id=tid, name="handback")


async def test_acceptance_event_fault_rolls_back_result_cost_and_cursor_then_replays_once(
    db, monkeypatch
):
    tid, _ = await prepared(db)
    driver = Driver([ROWS])
    advisor = Advisor(SqlWorkbenchStores(db), driver)
    original = WorkbenchRepository.append_event

    async def broken(self, **kwargs):
        if kwargs["event_type"] == "step/accepted":
            raise RuntimeError("acceptance event fault")
        return await original(self, **kwargs)

    monkeypatch.setattr(WorkbenchRepository, "append_event", broken)
    with pytest.raises(RuntimeError, match="acceptance event fault"):
        await advisor._step_once(tid)
    monkeypatch.setattr(WorkbenchRepository, "append_event", original)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["current_step"] == 0 and row["budget"]["used"] == "0.02"
        assert not await repo.get_artifact(task_id=tid, name=output_name("inspect_1"))
        aid = str(row["active_attempt_id"])
        assert (await repo.get_attempt(aid))["status"] == AttemptState.CREATED
        view = await execution_for_task(repo, row)
    step = view.pack.step(0)
    assessment = StepAssessment(judge(ROWS, step.acceptance))
    for _ in range(2):
        assert (
            await advisor._settle_step(
                tid, aid, view, step, driver.results[0], assessment, 0, None
            )
            == TaskState.QUEUED
        )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["current_step"] == 1 and row["budget"]["used"] == "0.03"
        assert (
            len([a for a in await repo.list_artifacts(tid) if a["kind"] == "step"]) == 1
        )


async def test_final_event_fault_rolls_back_handback_and_resumes_without_new_turn(
    db, monkeypatch
):
    tid, _ = await task(db)
    original = WorkbenchRepository.append_event

    async def broken(self, **kwargs):
        if (
            kwargs["event_type"] == "task/state"
            and kwargs["payload"].get("to") == TaskState.SUCCEEDED
        ):
            raise RuntimeError("final event fault")
        return await original(self, **kwargs)

    monkeypatch.setattr(WorkbenchRepository, "append_event", broken)
    with pytest.raises(RuntimeError, match="final event fault"):
        await Advisor(
            SqlWorkbenchStores(db), Driver([proposal().model_dump_json(), ROWS, ROWS])
        ).drive(tid)
    monkeypatch.setattr(WorkbenchRepository, "append_event", original)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["state"] == TaskState.QUEUED and row["current_step"] == 2
        assert not await repo.get_artifact(task_id=tid, name="handback")
    assert (
        await Advisor(SqlWorkbenchStores(db), Driver([])).drive(tid)
        == TaskState.SUCCEEDED
    )


async def test_old_completed_reply_cannot_release_a_later_cancelled_turn_hold(db):
    tid, _ = await prepared(db)
    driver = Driver([ROWS])
    advisor = Advisor(SqlWorkbenchStores(db), driver)
    assert await advisor._step_once(tid) == TaskState.QUEUED
    async with db() as session:
        repo = WorkbenchRepository(session)
        view = await execution_for_task(repo, await repo.get_task(tid))
        first = (await repo.list_attempts(tid))[-1]
    later = await open_step(db, tid)
    async with db() as session:
        await Ledger(WorkbenchRepository(session)).request_cancel(
            tid, owner_actor_id=OWNER
        )
    step = view.pack.step(0)
    assert (
        await advisor._settle_step(
            tid,
            str(first["id"]),
            view,
            step,
            driver.results[0],
            StepAssessment(judge(ROWS, step.acceptance)),
            0,
            None,
        )
        == TaskState.CANCELLED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert (
            row["original_input"]["client_context"]["turn_open"]["attempt_id"]
            == later["attempt_id"]
        )
        assert await Ledger(repo).release_turn(tid, attempt_id=later["attempt_id"])


async def test_old_reply_cannot_dismiss_later_turn_question(db):
    tid, _ = await prepared(db)
    driver = Driver([ROWS])
    advisor = Advisor(SqlWorkbenchStores(db), driver)
    await advisor._step_once(tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        view = await execution_for_task(repo, await repo.get_task(tid))
        first = (await repo.list_attempts(tid))[-1]
    later = await open_step(db, tid)
    async with db() as session:
        led = Ledger(WorkbenchRepository(session))
        await led.attempt_transition(later["attempt_id"], AttemptState.RUNNING)
        pending = await led.open_interaction(
            tid,
            kind="question",
            prompt=InteractionPrompt(
                title="请确认",
                question="如何继续？",
                subject={"request_id": "new-question"},
            ),
            waiting_reason=WaitingReason.INPUT,
            attempt_id=later["attempt_id"],
        )
    step = view.pack.step(0)
    assessment = await advisor._assess_step(
        tid, str(first["id"]), step, driver.results[0], "", None
    )
    assert (
        await advisor._settle_step(
            tid, str(first["id"]), view, step, driver.results[0], assessment, 0, None
        )
        == TaskState.WAITING
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert str(row["active_interaction_id"]) == pending["interaction_id"]
        assert (await repo.get_interaction(pending["interaction_id"]))[
            "status"
        ] == "pending"


async def test_existing_runner_protocol_executes_dynamic_task_from_received(db):
    from test_workbench_advisor_db import ScriptedAppServer, close, drive, seed

    settings = []

    class RecordingServer(ScriptedAppServer):
        async def handle(self, ws, msg):
            if msg["method"] == "turn/start":
                settings.append(msg["params"])
            await super().handle(ws, msg)

    fake = RecordingServer([proposal().model_dump_json(), ROWS, ROWS])
    await fake.start()
    runner = None
    try:
        runner, sid, sb, tid = await seed(db, fake)
        async with db() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE workbench_tasks SET pack_source='knowmart', pack_snapshot=cast(:p as jsonb), pack_digest=:d, profile_version='1' WHERE id=:id"
                    ),
                    {
                        "p": json.dumps(catalog().model_dump(mode="json")),
                        "d": "sha256:" + "a" * 64,
                        "id": tid,
                    },
                )
        await drive(runner)
        async with db() as session:
            row = await WorkbenchRepository(session).get_task(tid)
            assert row["state"] == TaskState.SUCCEEDED
            assert (
                row["acceptance_contract"]["runtime_contract"]
                == catalog().runtime_contract
            )
            assert row["current_step"] == 2
            attempts = await WorkbenchRepository(session).list_attempts(tid)
            assert len(attempts) == 3
            assert all(a["refs"]["runtime_turn_id"] in a["turn_ids"] for a in attempts)
        assert len(fake.turn_inputs) == 3
        assert settings[0]["sandboxPolicy"] == {
            "type": "readOnly",
            "networkAccess": False,
        }
        assert settings[0]["environments"] == []
        assert settings[0]["approvalPolicy"] == "never"
        assert all(p["sandboxPolicy"]["type"] == "workspaceWrite" for p in settings[1:])
    finally:
        if runner:
            await close(runner)
        await fake.close()


async def test_assessment_exception_still_charges_completed_turn_without_result(db):
    tid, _ = await task(db)

    class BrokenAssessment(Advisor):
        async def _assess_step(self, *args, **kwargs):
            raise RuntimeError("evidence reader crashed")

    driver = Driver([proposal().model_dump_json(), ROWS])
    assert (
        await BrokenAssessment(SqlWorkbenchStores(db), driver).drive(tid)
        == TaskState.FAILED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert Decimal(row["budget"]["used"]) == Decimal("0.02")
        assert Decimal(row["budget"]["reserved"]) == 0 and row["current_step"] == 0
        assert not await repo.get_artifact(task_id=tid, name=output_name("inspect_1"))


async def test_cancel_during_final_file_hashing_prevents_handback_and_delivery(db):
    tid, _ = await task(db)

    def with_file(d):
        next(u for u in d["method_units"] if u["contract"]["step_id"] == "report")[
            "contract"
        ]["output_file"] = "report.md"

    await patch_catalog(db, tid, with_file)

    class Files:
        async def exists(self, **kwargs):
            return True

        async def digest(self, **kwargs):
            async with db() as session:
                await asyncio.wait_for(
                    Ledger(WorkbenchRepository(session)).request_cancel(
                        tid, owner_actor_id=OWNER
                    ),
                    timeout=3,
                )
            return "a" * 64

    assert (
        await Advisor(
            SqlWorkbenchStores(db),
            Driver([proposal().model_dump_json(), ROWS, ROWS]),
            deliverables=Files(),
        ).drive(tid)
        == TaskState.CANCELLED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert not await repo.get_artifact(task_id=tid, name="handback")
        assert (await repo.get_task(tid))["terminal_result_ref"] is None


async def test_finalization_cannot_substitute_partial_results_for_required_final(db):
    tid, _ = await task(db)
    advisor = Advisor(
        SqlWorkbenchStores(db), Driver([proposal().model_dump_json(), ROWS])
    )
    await advisor._step_once(tid)
    await advisor._step_once(tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        view = await execution_for_task(repo, row)
        from app.domain.workbench.errors import MethodPlanInvalid

        with pytest.raises(MethodPlanInvalid, match="not all planned"):
            await advisor._finish(Ledger(repo), repo, row, view.pack)
        assert not await repo.get_artifact(task_id=tid, name="handback")


@pytest.mark.parametrize("exhausted", [False, True])
async def test_explicit_pin_rework_requires_bounded_new_plan_receipt(db, exhausted):
    tid, _ = await task(db)
    if exhausted:
        await patch_catalog(db, tid, lambda d: d["planning"].update(max_replans=1))
    driver = Driver([proposal().model_dump_json(), ROWS])
    advisor = Advisor(SqlWorkbenchStores(db), driver)
    await advisor._step_once(tid)
    await advisor._step_once(tid)
    pinned = proposal(parent=1).model_dump(mode="json")
    pinned["steps"][1]["inputs"][0]["version"] = 1
    driver.replies.append(json.dumps(pinned))
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid, parent_version=1)
        == TaskState.QUEUED
    )
    driver.replies.extend(["{}", "{}", OTHER_ROWS])
    if not exhausted:
        rebound = json.loads(json.dumps(pinned))
        rebound["parent_version"] = 2
        rebound["steps"][1]["inputs"][0]["version"] = 2
        driver.replies.extend([json.dumps(rebound), OTHER_ROWS])
    assert await advisor.drive(tid) == (
        TaskState.FAILED if exhausted else TaskState.SUCCEEDED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        old = await read_plan_version(repo, row, 2)
        assert old.proposal.steps[1].inputs[0].version == 1
        assert Decimal(row["budget"]["reserved"]) == 0
        attempts = await repo.list_attempts(tid)
        failures = [
            a
            for a in attempts
            if a["step_id"] == "report_1" and a["status"] == AttemptState.FAILED
        ]
        assert len(failures) == 2
        assert all(a["input_artifact_versions"][0]["version"] == 1 for a in failures)
        assert len(driver.prompts) == (6 if exhausted else 8)
        assert Decimal(row["budget"]["used"]) == Decimal("0.01") * len(driver.prompts)
        if not exhausted:
            assert row["accepted_plan_version"] == 3
            assert attempts[-1]["input_artifact_versions"][0]["version"] == 2
            assert (
                '"adjustment_reason": "pinned input was invalidated by rework"'
                in driver.prompts[-2]
            )


async def test_binding_changed_during_assessment_rejects_result_but_settles_cost(db):
    tid, _ = await task(db)

    class Tampered(Advisor):
        async def _assess_step(self, *args, **kwargs):
            assessment = await super()._assess_step(*args, **kwargs)
            async with db() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "UPDATE workbench_attempts SET refs=refs || '{\"tools\":[\"shell\"]}'::jsonb WHERE task_id=:tid AND status='RUNNING'"
                        ),
                        {"tid": tid},
                    )
            return assessment

    # Scripted Driver does not itself transition CREATED -> RUNNING.
    # Corrupt the active binding irrespective of that transient status.
    class CorruptDriver(Driver):
        async def run_turn(self, thread_id, prompt, *, timeout):  # noqa: ASYNC109
            result = await super().run_turn(thread_id, prompt, timeout=timeout)
            if len(self.prompts) == 2:
                async with db() as session:
                    repo = WorkbenchRepository(session)
                    row = await repo.get_task(tid)
                    await Ledger(repo).attempt_transition(
                        str(row["active_attempt_id"]), AttemptState.RUNNING
                    )
            return result

    assert (
        await Tampered(
            SqlWorkbenchStores(db), CorruptDriver([proposal().model_dump_json(), ROWS])
        ).drive(tid)
        == TaskState.FAILED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert Decimal(row["budget"]["used"]) == Decimal("0.02")
        assert Decimal(row["budget"]["reserved"]) == 0
        assert not await repo.get_artifact(task_id=tid, name=output_name("inspect_1"))
