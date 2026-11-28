"""固定专家的计划回执落库（SDD 0050 第二、四节）：只给没开始的新委托写，不开尝试，改了就读不出。"""

from decimal import Decimal

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_task_runtime_db import ScriptedAppServer, close, seed

from app.application.workbench.fixed_plans import (
    FIXED_PLAN_ATTEMPT,
    accept_fixed_plan,
    fixed_pack_digest,
)
from app.application.workbench.ledger import Ledger
from app.application.workbench.plan_receipts import read_plan
from app.domain.workbench.errors import MethodPlanInvalid
from app.domain.workbench.fixed_plan import fixed_plan
from app.domain.workbench.method_plan import PLAN_ARTIFACT
from app.domain.workbench.packs import SMOKE
from app.infrastructure.workbench.repository import WorkbenchRepository


async def _seeded(db):
    fake = ScriptedAppServer([])
    await fake.start()
    runner, _, _, tid = await seed(db, fake)
    return fake, runner, tid


async def test_a_new_fixed_task_gets_one_receipt_and_no_attempt(db):
    fake, runner, tid = await _seeded(db)
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            before = await repo.get_task(tid)
            receipt = await accept_fixed_plan(
                repo, tid, expected_version=before["state_version"]
            )
            assert receipt is not None
            assert receipt.proposal == fixed_plan(SMOKE)
            assert receipt.attempt_id == FIXED_PLAN_ATTEMPT
            assert receipt.pack_digest == fixed_pack_digest(before, SMOKE)
            task = await repo.get_task(tid)
            assert task["accepted_plan_version"] == 1
            assert task["state"] == before["state"]  # 委托状态不变
            assert await repo.list_attempts(tid) == []  # 不开尝试，不进时间线
            [artifact] = [
                a for a in await repo.list_artifacts(tid) if a["name"] == PLAN_ARTIFACT
            ]
            assert artifact["attempt_id"] is None
            assert await read_plan(repo, task) == receipt
            # 只写一次
            assert (
                await accept_fixed_plan(
                    repo, tid, expected_version=task["state_version"]
                )
                is None
            )
    finally:
        await close(runner)
        await fake.close()


async def test_a_task_already_on_the_old_path_is_not_repinned(db):
    fake, runner, tid = await _seeded(db)
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            await Ledger(repo).open_attempt(
                tid, step_id="plan", step_version="1", reserve=Decimal("0.05")
            )
            task = await repo.get_task(tid)
            assert (
                await accept_fixed_plan(
                    repo, tid, expected_version=task["state_version"]
                )
                is None
            )
            assert (await repo.get_task(tid))["accepted_plan_version"] is None
    finally:
        await close(runner)
        await fake.close()


async def test_a_changed_receipt_no_longer_reads(db):
    fake, runner, tid = await _seeded(db)
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(tid)
            await accept_fixed_plan(repo, tid, expected_version=task["state_version"])
            await s.execute(
                text(
                    "update workbench_artifacts set content = jsonb_set(content,"
                    " '{proposal,final_outputs}', '{\"plan\": \"plan\"}')"
                    " where task_id = :t and name = :n"
                ),
                {"t": tid, "n": PLAN_ARTIFACT},
            )
            await s.commit()
            with pytest.raises(MethodPlanInvalid):
                await read_plan(repo, await repo.get_task(tid))
    finally:
        await close(runner)
        await fake.close()


async def test_queue_and_fixed_receipt_rollback_together(db, monkeypatch):
    from unittest.mock import AsyncMock

    from app.application.workbench import task_runtime
    from app.domain.workbench.states import TaskState
    from app.infrastructure.workbench.repository import SqlWorkbenchStores

    fake, runner, tid = await _seeded(db)
    try:
        runtime = task_runtime.TaskRuntime(
            SqlWorkbenchStores(db),
            None,
            fixed_as_plan_owners=frozenset({"fixed-owner"}),
        )
        monkeypatch.setattr(
            WorkbenchRepository,
            "subject_of_actor",
            AsyncMock(return_value="fixed-owner"),
        )

        async def interrupted(repo, tid, **kwargs):
            assert (await repo.get_task(tid))["state"] == TaskState.QUEUED
            raise RuntimeError("interrupted before receipt")

        with monkeypatch.context() as patch:
            patch.setattr(task_runtime, "accept_fixed_plan", interrupted)
            with pytest.raises(RuntimeError, match="interrupted before receipt"):
                await runtime.drive(tid)
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(tid)
            assert (
                task["state"] == TaskState.VALIDATING
                and task["accepted_plan_version"] is None
            )
        monkeypatch.setattr(
            runtime, "_step_once", AsyncMock(return_value=TaskState.WAITING)
        )
        await runtime.drive(tid)
        async with db() as session:
            repo = WorkbenchRepository(session)
            task = await repo.get_task(tid)
            assert task["state"] == TaskState.QUEUED and (
                await read_plan(repo, task)
            ).proposal == fixed_plan(SMOKE)
    finally:
        await close(runner)
        await fake.close()
