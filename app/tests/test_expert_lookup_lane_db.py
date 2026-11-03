"""Independent market lookup and project execution, using the real ledger/database."""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import OWNER, handover, seed
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.find_expert import FindExpertService
from app.application.workbench.ledger import Ledger
from app.application.workbench.project_service import ProjectService
from app.application.workbench.session_service import SessionService
from app.domain.workbench.errors import ProjectBusy
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.scheduling import LOOKUP_PROFILE
from app.domain.workbench.states import TaskState, WaitingReason
from app.infrastructure.workbench.repository import WorkbenchRepository


async def execution(repo, project, key):
    session = await SessionService(repo).start(
        owner_actor_id=OWNER, kind="expert", project_id=project
    )
    return await Ledger(repo).handover(
        handover(session["session_id"], key=key), owner_actor_id=OWNER
    )


@pytest.mark.parametrize("waiting", [False, True])
async def test_lookup_runs_beside_analysis_but_each_lane_remains_serial(db, waiting):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        pid = str((await repo.get_session(sid))["project_id"])
        work = await led.handover(handover(sid), owner_actor_id=OWNER)
        for state in (TaskState.VALIDATING, TaskState.QUEUED, TaskState.RUNNING):
            await led.transition(work["task_id"], state)
        if waiting:
            await led.transition(
                work["task_id"], TaskState.WAITING, waiting_reason=WaitingReason.DATA
            )
        svc = FindExpertService(repo)
        find = await svc.start(
            owner_actor_id=OWNER,
            project_id=pid,
            question="选择专家",
            idempotency_key="find-lane-first",
        )
        lookup = await repo.get_task(find["task_id"])
        assert not lookup["original_input"]["client_context"].get("queued_behind")
        assert lookup["budget"]["limit"] == "2"
        assert (await repo.get_session(find["session_id"]))["wheel"] == "advisor"
        next_work = await execution(repo, pid, "work-lane-next")
        next_find = await svc.start(
            owner_actor_id=OWNER,
            project_id=pid,
            question="另找一位",
            idempotency_key="find-lane-next",
        )
        assert next_work["queued_behind"] == work["task_id"]
        queued_view = await ExpertDesk(repo).steps(
            next_work["task_id"], owner_actor_id=OWNER
        )
        assert queued_view["position"] is None
        assert queued_view["now"] is None
        assert all(step["status"] == "pending" for step in queued_view["steps"])
        assert (await repo.get_task(next_find["task_id"]))["original_input"][
            "client_context"
        ]["queued_behind"] == find["task_id"]
        view = await ProjectService(repo).view(pid, owner_actor_id=OWNER)
        assert view["active_task_id"] == work["task_id"]
        with pytest.raises(ProjectBusy):
            await SessionService(repo).archive(find["session_id"], owner_actor_id=OWNER)
        # Cancelling lookup only advances lookup, never the blocked execution.
        await led.request_cancel(find["task_id"], owner_actor_id=OWNER)
        after = await repo.get_task(next_find["task_id"])
        assert not after["original_input"]["client_context"].get("queued_behind")
        assert (await repo.get_task(work["task_id"]))["state"] == (
            "WAITING" if waiting else "RUNNING"
        )
        assert (await repo.get_task(next_work["task_id"]))["original_input"][
            "client_context"
        ]["queued_behind"] == work["task_id"]
        assert (await repo.get_session(sid))["wheel"] == "advisor"


async def test_lookup_does_not_block_execution_or_interactive_work(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        pid = str((await repo.get_session(sid))["project_id"])
        find = await FindExpertService(repo).start(
            owner_actor_id=OWNER,
            project_id=pid,
            question="选择专家",
            idempotency_key="lookup-first",
        )
        assert (await ProjectService(repo).view(pid, owner_actor_id=OWNER))[
            "active_task_id"
        ] is None
        await SessionService(repo).assert_user_may_drive(sid, owner_actor_id=OWNER)
        work = await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER)
        assert work["queued_behind"] is None
        assert (
            str((await repo.active_task_of_project(pid, lane="lookup"))["id"])
            == find["task_id"]
        )


async def test_legacy_lookup_queued_behind_analysis_recovers_via_ledger(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        pid = str((await repo.get_session(sid))["project_id"])
        work = await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER)
        # Seed the pre-fix queued shape, then use only the production recovery path.
        pending = await execution(repo, pid, "legacy-find")
        await s.execute(
            text("update workbench_tasks set profile_id=:profile where id=:id"),
            {"profile": LOOKUP_PROFILE, "id": pending["task_id"]},
        )
        await s.commit()
        assert pid in await repo.projects_with_stalled_queue()
        assert await Ledger(repo).start_next_queued(pid) == pending["task_id"]
        assert await Ledger(repo).start_next_queued(pid) is None
        task = await repo.get_task(pending["task_id"])
        assert not task["original_input"]["client_context"].get("queued_behind")
        assert (
            str((await repo.active_task_of_project(pid, lane="execution"))["id"])
            == work["task_id"]
        )


async def test_client_context_cannot_select_the_lookup_lane(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        pid = str((await repo.get_session(sid))["project_id"])
        work = await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER)
        other = await SessionService(repo).start(
            owner_actor_id=OWNER, kind="expert", project_id=pid
        )
        forged = await Ledger(repo).handover(
            HandoverRequest(
                idempotency_key="fake-find-lane",
                session_id=other["session_id"],
                profile_id="DATA_QUERY",
                original_input={"text": "q"},
                budget_limit=Decimal("2"),
                client_context={"entry": "find-expert", "lane": "lookup"},
            ),
            owner_actor_id=OWNER,
        )
        assert forged["queued_behind"] == work["task_id"]


async def test_concurrent_lookup_submissions_take_only_one_lane_slot(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        pid = str((await repo.get_session(sid))["project_id"])
        await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER)

    async def submit(key):
        async with db() as s:
            return await FindExpertService(WorkbenchRepository(s)).start(
                owner_actor_id=OWNER,
                project_id=pid,
                question="选择专家",
                idempotency_key=key,
            )

    pairs = await asyncio.gather(
        submit("parallel-find-one"), submit("parallel-find-two")
    )
    async with db() as s:
        repo = WorkbenchRepository(s)
        tasks = [await repo.get_task(p["task_id"]) for p in pairs]
        assert (
            sum(
                bool(t["original_input"]["client_context"].get("queued_behind"))
                for t in tasks
            )
            == 1
        )
        assert len({str(t["session_id"]) for t in tasks}) == 2
