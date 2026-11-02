"""Dynamic views use dispatch receipts; historical results stay distinct."""

import json

import pytest
from method_plan_fixtures import catalog, proposal
from test_method_advisor_db import OTHER_ROWS, ROWS, Driver, patch_catalog
from test_method_plans_db import task
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.advisor import Advisor
from app.application.workbench.execution_plan import current_artifacts
from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.ledger import Ledger
from app.application.workbench.method_planner import MethodPlanner
from app.application.workbench.run_files import task_dossier
from app.domain.workbench.errors import NotFound
from app.domain.workbench.method_plan import output_name
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

pytestmark = pytest.mark.asyncio


async def test_unplanned_view_and_active_planner_do_not_invent_method_steps(db):
    tid, _ = await task(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        desk = ExpertDesk(repo)
        before = await desk.steps(tid, owner_actor_id=OWNER)
        assert before["steps"] == [] and before["position"] is None
        assert before["task"]["expert"]["name"] == catalog().name
        assert before["task"]["plan"] == {
            "version": None,
            "planning": False,
            "reason": None,
            "history": [],
        }
        assert (await desk.dossier(tid, owner_actor_id=OWNER))["sections"] == []
        row = await repo.get_task(tid)
        await Ledger(repo).open_method_plan(tid, expected_version=row["state_version"])
        active = await desk.steps(tid, owner_actor_id=OWNER)
        assert active["task"]["plan"]["planning"] is True
        assert active["steps"] == []
        assert active["now"]["doing"]["code"] == "planning"
        assert await task_dossier(repo, await repo.get_task(tid)) is None
        with pytest.raises(NotFound):
            await desk.steps(tid, owner_actor_id="00000000-0000-0000-0000-000000000099")


async def test_steps_and_dossier_use_selected_instances_and_all_current_finals(db):
    tid, _ = await task(db)
    await patch_catalog(
        db, tid, lambda d: d["planning"].update(final_units=["inspect", "report"])
    )
    raw = proposal(review=True).model_dump(mode="json")
    raw["final_outputs"]["inspect"] = "inspect_1"
    assert (
        await Advisor(
            SqlWorkbenchStores(db),
            Driver([json.dumps(raw), ROWS, '{"notes":"checked"}', ROWS]),
        ).drive(tid)
        == TaskState.SUCCEEDED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=None,
                name=output_name("report_1"),
                kind="step",
                content=json.loads(OTHER_ROWS),
            )
        desk = ExpertDesk(repo)
        shown = await desk.steps(tid, owner_actor_id=OWNER)
        assert [s["method_instance"] for s in shown["steps"]] == [
            "inspect_1",
            "review_1",
            "report_1",
        ]
        assert all(s["current_result"]["version"] == 1 for s in shown["steps"])
        assert shown["task"]["plan"]["version"] == 1
        assert len(shown["task"]["plan"]["history"]) == 1
        assert shown["steps"][1]["selection_reason"] == raw["steps"][1]["reason"]
        paper = await desk.dossier(tid, owner_actor_id=OWNER)
        blocks = [b for section in paper["sections"] for b in section["blocks"]]
        assert [b["artifact"]["name"] for b in blocks] == [
            output_name("inspect_1"),
            output_name("report_1"),
        ]
        assert all("one" in b["content"] and "two" not in b["content"] for b in blocks)
        current = await current_artifacts(repo, await repo.get_task(tid))
        assert current[output_name("report_1")]["version"] == 1


async def test_rewound_results_remain_in_history_but_are_not_current_evidence(db):
    tid, _ = await task(db)
    await patch_catalog(
        db, tid, lambda d: d["planning"].update(final_units=["inspect", "report"])
    )
    raw = proposal().model_dump(mode="json")
    raw["final_outputs"]["inspect"] = "inspect_1"
    driver = Driver([json.dumps(raw), ROWS, "{}", "{}"])
    advisor = Advisor(SqlWorkbenchStores(db), driver)
    for _ in range(4):
        assert await advisor._step_once(tid) == TaskState.QUEUED
    async with db() as session:
        repo = WorkbenchRepository(session)
        desk = ExpertDesk(repo)
        shown = await desk.steps(tid, owner_actor_id=OWNER)
        assert all(s["current_result"] is None for s in shown["steps"])
        assert shown["steps"][0]["attempts"][0]["outcome"] == "accepted"
        paper = await desk.dossier(tid, owner_actor_id=OWNER)
        assert all(
            b["status"] == "not_reached"
            for section in paper["sections"]
            for b in section["blocks"]
        )
        detail = await desk.step(tid, 1, owner_actor_id=OWNER)
        assert detail["step"]["attempts"][0]["returned"]["version"] == 1


async def test_plan_history_and_method_files_follow_current_plan(db):
    tid, _ = await task(db)

    def files(d):
        for unit in d["method_units"]:
            if unit["contract"]["step_id"] == "review":
                unit["contract"]["output_file"] = "review.md"

    await patch_catalog(db, tid, files)
    driver = Driver([proposal(review=True).model_dump_json()])
    advisor = Advisor(SqlWorkbenchStores(db), driver)
    await advisor._step_once(tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        dossier = await task_dossier(repo, await repo.get_task(tid))
        assert any("review.md" in str(f) for f in dossier["files"])
    driver.replies.append(proposal(parent=1).model_dump_json())
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid, parent_version=1)
        == TaskState.QUEUED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        shown = await ExpertDesk(repo).steps(tid, owner_actor_id=OWNER)
        assert [h["version"] for h in shown["task"]["plan"]["history"]] == [1, 2]
        assert len(shown["steps"]) == 2
        assert await task_dossier(repo, await repo.get_task(tid)) is None
