"""Real ledger transactions: matching requirements remain authoritative in planning."""

import json
from decimal import Decimal

import pytest
from test_capability_method_plan import document, proposal
from test_expert_match import QUESTION
from test_expert_match import result as scope_result
from test_expert_match import validate as match
from test_method_planner_db import Driver
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.catalog import PinnedTestCatalog
from app.application.workbench.execution_plan import execution_for_task
from app.application.workbench.ledger import Ledger
from app.application.workbench.method_planner import MethodPlanner
from app.application.workbench.plan_receipts import read_plan
from app.application.workbench.task_requirements import for_task
from app.application.workbench.turn import TurnResult
from app.domain.workbench.errors import IdempotencyConflict, MethodPlanInvalid
from app.domain.workbench.method_catalog import parse_catalog
from app.domain.workbench.states import TaskState
from app.domain.workbench.task_requirements import from_match
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)

pytestmark = pytest.mark.asyncio
DIGEST = "sha256:" + "a" * 64


def requirements():
    return from_match(
        match(),
        match_task_id="synthetic-match",
        match_artifact_version=1,
        knowbot_id="synthetic",
        version="1",
        expert_digest=DIGEST,
        question=QUESTION,
    )


async def create(db, *, claim=True):
    _, _, sid = await seed(db)
    req = handover(sid, key=f"capability-{sid}", text=QUESTION)
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        made = await led.handover(
            req,
            owner_actor_id=OWNER,
            snapshot=PinnedTestCatalog(parse_catalog(document()), DIGEST),
            task_requirements=requirements() if claim else None,
        )
        tid = made["task_id"]
        await led.transition(tid, TaskState.VALIDATING)
        await led.transition(tid, TaskState.QUEUED)
    return tid, req


def reply(*, omit=False):
    doc = proposal(review=True)
    doc["requirement_coverage"] = {
        "r1": "double_check",
        **({} if omit else {"r2": "double_check"}),
    }
    return TurnResult("synthetic-plan", json.dumps(doc), {}, cost=Decimal("0.02"))


async def test_capability_intake_planner_and_execution_share_requirements(db):
    tid, _ = await create(db)
    driver = Driver(reply())
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid) == TaskState.QUEUED
    )
    assert '"task_requirements"' in driver.calls[0][1]
    assert '"input_slots"' in driver.calls[0][1]
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        plan = await read_plan(repo, task)
        assert plan.requirements_digest == requirements().digest()
        assert plan.proposal.requirement_coverage == {
            "r1": "double_check",
            "r2": "double_check",
        }
        view = await execution_for_task(repo, task)
        assert len(view.pack.workflow) == 3
        assert task["budget"]["used"] == "0.02"


async def test_plan_cannot_drop_one_matching_requirement_even_on_retry(db):
    tid, _ = await create(db)
    planner = MethodPlanner(SqlWorkbenchStores(db), Driver(reply(omit=True)))
    assert await planner.run(tid) == TaskState.QUEUED
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        assert task["accepted_plan_version"] is None
        assert task["budget"]["used"] == "0.02"
        assert len(await repo.list_task_events(tid, types=("task/plan_rejected",))) == 1
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), Driver(reply())).run(tid)
        == TaskState.QUEUED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        assert (
            await read_plan(repo, task)
        ).requirements_digest == requirements().digest()
        assert task["budget"]["used"] == "0.04"


async def test_unpublished_author_test_checks_scope_in_the_existing_planner_turn(db):
    tid, request = await create(db, claim=False)
    scope = scope_result()
    scope["picks"][0]["knowbot_id"] = tid
    for row in scope["picks"][0]["coverage"]:
        row["declaration_quote"] = "核对并复核给定文档"
    output = {"scope": scope, "plan": json.loads(reply().final_text)}
    driver = Driver(
        TurnResult("author-scope-plan", json.dumps(output), {}, cost=Decimal("0.03"))
    )
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid) == TaskState.QUEUED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        assert (await read_plan(repo, task)).requirements_digest
        assert task["original_input"]["task_requirements"]["match_task_id"] == tid
        assert (await repo.get_artifact(task_id=tid, name="_author_test_scope"))[
            "version"
        ] == 1
        assert task["budget"]["used"] == "0.03"
        assert (await Ledger(repo).handover(request, owner_actor_id=OWNER))[
            "task_id"
        ] == tid
    assert len(driver.calls) == 1


async def test_uncovered_author_question_never_executes_a_method(db):
    tid, _ = await create(db, claim=False)
    scope = scope_result()
    scope.update(
        outcome="none",
        picks=[],
        recommend={"action": "incubate", "reason": "需要另一种方法"},
    )
    driver = Driver(
        TurnResult(
            "uncovered",
            json.dumps({"scope": scope, "plan": None}),
            {},
            cost=Decimal("0.01"),
        )
    )
    assert (
        await MethodPlanner(SqlWorkbenchStores(db), driver).run(tid) == TaskState.FAILED
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        assert await read_plan(repo, task) is None
        assert task["budget"]["used"] == "0.01"


async def test_missing_author_requirements_cannot_bypass_execution_validation(db):
    tid, _ = await create(db, claim=False)
    async with db() as session:
        task = await WorkbenchRepository(session).get_task(tid)
        with pytest.raises(MethodPlanInvalid, match="server-pinned"):
            for_task(task, parse_catalog(document()))


async def test_same_request_cannot_replay_with_a_different_matching_receipt(db):
    tid, req = await create(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        assert (
            await led.handover(
                req, owner_actor_id=OWNER, task_requirements=requirements()
            )
        )["task_id"] == tid
        different = requirements().model_copy(update={"match_artifact_version": 2})
        with pytest.raises(IdempotencyConflict):
            await led.handover(req, owner_actor_id=OWNER, task_requirements=different)
