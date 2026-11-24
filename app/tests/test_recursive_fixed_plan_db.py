"""Both recursive planning entry points retain the node contract after plan binding."""

import json

import pytest
from test_hierarchical_planning import setup
from test_method_task_runtime_db import Driver, SqlWorkbenchStores
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.fixed_plans import accept_fixed_plan
from app.application.workbench.ledger import Ledger
from app.application.workbench.task_runtime import TaskRuntime
from app.domain.workbench import hierarchical_planning as htn
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("author", [True, False], ids=["incubation", "temporary-plan"])
async def test_bound_planner_dispatches_recursive_decision_contract(db, author):
    context, _ = setup(author=author)
    profile, version = ("INCUBATE", "2") if author else ("FIND_EXPERT", "7")
    question = {**context["original_input"]["input"], "planner": htn.PROTOCOL}
    _, _, sid = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        ledger = Ledger(repo)
        req = handover(sid).model_copy(
            update={
                "profile_id": profile,
                "profile_version": version,
                "original_input": question,
            }
        )
        tid = (await ledger.handover(req, owner_actor_id=OWNER))["task_id"]
        await ledger.transition(tid, TaskState.VALIDATING)
        await ledger.transition(tid, TaskState.QUEUED)
        task = await repo.get_task(tid)
        assert htn.active(task)
        planner = htn.HierarchicalPlanner(task)
        rules, _ = planner.model_contract(planner.prompt())
        assert (
            await accept_fixed_plan(repo, tid, expected_version=task["state_version"])
            is not None
        )
    driver = Driver(
        [
            json.dumps(
                {
                    "action": "inspect",
                    "methods": [next(iter(planner.methods))],
                    "pending_choice": None,
                }
            ),
            json.dumps({"action": "clarify", "reason": "请确认采用哪个版本？"}),
        ]
    )
    runtime = TaskRuntime(SqlWorkbenchStores(db), driver)
    await runtime._step_once(tid)
    assert len(driver.prompts) == 1 and rules in driver.prompts[0]
    async with db() as session:
        repo = WorkbenchRepository(session)
        [attempt] = await repo.list_attempts(tid)
        assert attempt["refs"]["planning_turn"] == 0
        assert attempt["status"] == "COMPLETED"
    await runtime._step_once(tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        attempts = await repo.list_attempts(tid)
        assert sorted(a["refs"]["planning_turn"] for a in attempts) == [0, 1]
        assert all(a["status"] == "COMPLETED" for a in attempts)
        assert len(driver.prompts) == 2


async def test_same_session_replan_gets_a_distinct_method_call(db):
    from sqlalchemy import text

    from app.application.workbench.method_calls import step_call
    from app.domain.workbench.capabilities import digest

    _, _, sid = await seed(db)
    meta = dict(
        plan_version=1,
        plan_digest="plan",
        pack_digest="pack",
        instance_id="root",
        method_id="selection",
        method_version="1",
        step_index=0,
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        ledger = Ledger(repo)
        first = await ledger.handover(handover(sid), owner_actor_id=OWNER)
        await ledger.transition(first["task_id"], TaskState.CANCELLED)
        second = await ledger.handover(
            handover(sid, key="new-round", text="修订范围"), owner_actor_id=OWNER
        )
        async with repo.transaction():
            original = await repo.get_task(first["task_id"], for_update=True)
            revised = await repo.get_task(second["task_id"], for_update=True)
            first_call = await step_call(repo, original, meta)
            # Model an in-flight attempt written before invocation-scoped digests.
            await session.execute(
                text("UPDATE workbench_calls SET digest=:digest WHERE id=:id"),
                {"digest": digest(meta), "id": first_call["id"]},
            )
            assert (await step_call(repo, original, meta))["id"] == first_call["id"]
            new_call = await step_call(repo, revised, meta)
            assert new_call["id"] != first_call["id"]
            assert new_call["parent_call_id"] != first_call["parent_call_id"]
            assert (await step_call(repo, revised, meta))["id"] == new_call["id"]
