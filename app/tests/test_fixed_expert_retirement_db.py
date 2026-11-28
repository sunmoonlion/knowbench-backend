"""Retired expert tasks have no user-facing receipt and cannot acquire an attempt."""

from decimal import Decimal

import pytest
from knowbot_fixtures import manifest as retired_manifest
from sqlalchemy import text
from test_dynamic_incubation_db import ScriptedAppServer, admit
from test_knowbot_test_runs_db import request, runner_for, setup_test_computer
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.execution_plan import (
    display_for_task,
    execution_for_task,
)
from app.application.workbench.ledger import Ledger
from app.domain.workbench.errors import KnowbotInvalid, MethodPlanInvalid, NotFound
from app.domain.workbench.packs import SMOKE
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("source", ["knowmart", "knowlab-test"])
@pytest.mark.parametrize("command", ["task.drive", "task.reconnect"])
async def test_retired_snapshot_never_dispatches_or_reserves_money(db, source, command):
    _, _, sid = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        task_id = (await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER))[
            "task_id"
        ]
        await session.execute(
            text("""
            update workbench_tasks set pack_source=:source, pack_snapshot=cast(:snapshot as jsonb),
            pack_digest=:digest where id=:id
        """),
            dict(
                source=source,
                snapshot=SMOKE.model_dump_json(),
                digest="sha256:" + "a" * 64,
                id=task_id,
            ),
        )
        await repo.put_artifact(
            task_id=task_id,
            attempt_id=None,
            name="archived-report",
            kind="step",
            content={"total": 42},
        )
        await session.commit()
        with pytest.raises(KnowbotInvalid, match="retired"):
            await Ledger(repo).open_attempt(
                task_id, step_id="old", step_version="1", reserve=Decimal("1")
            )
        task = await repo.get_task(task_id)
        with pytest.raises(MethodPlanInvalid, match="retired"):
            await execution_for_task(repo, task)
        assert await display_for_task(repo, task) is None
    runner = runner_for(db)
    await runner.handle(
        {"session_id": sid, "kind": command, "payload": {"task_id": task_id}}
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(task_id)
        assert TaskState(task["state"]) == TaskState.CANCELLED
        assert await repo.list_attempts(task_id) == []
        browser = WorkbenchRepository(session, browser_view=True)
        with pytest.raises(NotFound):
            await browser.get_task(task_id, owner_actor_id=OWNER)
        with pytest.raises(NotFound):
            await browser.get_session(sid, owner_actor_id=OWNER)
        assert not await browser.list_tasks(owner_actor_id=OWNER)
        assert not await browser.list_sessions(owner_actor_id=OWNER)
        assert await display_for_task(repo, task) is None


async def test_fixed_author_trial_is_refused_even_for_admitted_owner(db):
    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        req = request("retired-fixed-trial", doc=retired_manifest())
        with pytest.raises(KnowbotInvalid, match="retired"):
            await admit(db, req)
        async with db() as session:
            assert await WorkbenchRepository(session).get_test_run(req.run_id) is None
    finally:
        await fake.close()


async def test_previously_accepted_fixed_trial_is_closed_before_work_starts(db):
    from test_dynamic_incubation_db import dynamic_request

    from app.application.workbench.test_runs import KnowbotTestConductor
    from app.bootstrap.workbench import workbench_stores

    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        old = request("previous-fixed-trial", doc=retired_manifest())
        await admit(db, dynamic_request(old.run_id))
        async with db() as session:
            await session.execute(
                text(
                    "update workbench_test_runs set request=cast(:body as jsonb), digest=:digest where run_id=:id"
                ),
                {
                    "body": old.model_dump_json(),
                    "digest": old.digest(),
                    "id": old.run_id,
                },
            )
            await session.commit()
        await KnowbotTestConductor(workbench_stores(db)).advance(old.run_id)
        async with db() as session:
            repo = WorkbenchRepository(session)
            run = await repo.get_test_run(old.run_id)
            assert run["status"] == "cancelled" and Decimal(run["spent"]) == 0
            from app.application.workbench.test_runs import KnowbotTestService

            service = KnowbotTestService(repo)
            with pytest.raises(NotFound):
                await service.status(old.run_id)
            with pytest.raises(NotFound):
                await service.outputs(old.run_id, "case_1")
            [case] = await repo.list_test_cases(old.run_id)
            assert case["task_id"] is None and "retired" in case["result"]["note"]
    finally:
        await fake.close()


async def test_mixed_thread_exposes_only_current_task_evidence(db):
    _, _, sid = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        ledger = Ledger(repo)
        old_id = (await ledger.handover(handover(sid), owner_actor_id=OWNER))["task_id"]
        await ledger.transition(old_id, TaskState.CANCELLED)
        await session.execute(
            text(
                "update workbench_tasks set profile_id='FIND_EXPERT', profile_version='4' where id=:id"
            ),
            {"id": old_id},
        )
        await session.commit()
        new_id = (
            await ledger.handover(
                handover(sid, key="current-key-2"), owner_actor_id=OWNER
            )
        )["task_id"]
        for task, marker in (
            (old_id, "retired"),
            (None, "unattributed"),
            (new_id, "current"),
        ):
            await repo.append_event(
                session_id=sid,
                kind="runtime",
                event_type="item/completed",
                payload={"marker": marker},
                task_id=task,
            )
        browser = WorkbenchRepository(session, browser_view=True)
        assert str((await browser.get_session(sid, owner_actor_id=OWNER))["id"]) == sid
        assert (
            str((await browser.get_task(new_id, owner_actor_id=OWNER))["id"]) == new_id
        )
        with pytest.raises(NotFound):
            await browser.get_task(old_id, owner_actor_id=OWNER)
        for events in (
            await browser.list_events(session_id=sid),
            await repo.list_record_events(
                session_id=sid, types=("item/completed",), visible_only=True
            ),
        ):
            assert [
                event["payload"]["marker"]
                for event in events
                if "marker" in event["payload"]
            ] == ["current"]
