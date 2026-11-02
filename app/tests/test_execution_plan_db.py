"""Task-local projection, dispatch and lineage under real database transactions."""

import asyncio
from decimal import Decimal

import pytest
from method_plan_fixtures import proposal
from test_method_plans_db import accept, planned_attempt, task
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.execution_plan import (
    accepted_results,
    active_method_step,
    execution_for_task,
)
from app.application.workbench.ledger import Ledger
from app.domain.workbench.errors import MethodPlanInvalid, StaleStateVersion
from app.domain.workbench.method_plan import output_name
from app.domain.workbench.states import AttemptState, TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


async def prepared(db, *, plan=None):
    tid, sid = await task(db)
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version, plan)
    return tid, sid


async def open_step(db, tid, version=None):
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        return await Ledger(repo).open_method_step(
            tid, expected_version=version or row["state_version"]
        )


async def complete(db, tid, opened, *, value="first"):
    async with db() as session:
        repo = WorkbenchRepository(session)
        led = Ledger(repo)
        await led.attempt_transition(opened["attempt_id"], AttemptState.RUNNING)
        async with repo.transaction():
            art = await repo.put_artifact(
                task_id=tid,
                attempt_id=opened["attempt_id"],
                name=opened["step"].output_artifact,
                kind="step",
                content={"rows": [value]},
            )
            await led.attempt_transition(
                opened["attempt_id"],
                AttemptState.COMPLETED,
                consumed=Decimal("0.01"),
                output_artifacts=[{k: art[k] for k in ("id", "name", "version")}],
            )
            row = await repo.get_task(tid)
            await repo.cas_task(
                tid,
                expected_version=row["state_version"],
                current_step=int(row["current_step"]) + 1,
            )
        return art


async def test_dispatch_restart_inputs_permissions_and_budget_are_bound(db):
    tid, _ = await prepared(db)
    first = await open_step(db, tid)
    assert first["step"].step_id == "inspect_1"
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        view = await execution_for_task(repo, row)
        assert (await active_method_step(repo, row, view)).tools == ("read_input",)
        a = await repo.get_attempt(first["attempt_id"])
        assert a["refs"]["plan_version"] == 1 and a["refs"]["method_id"] == "inspect"
        assert a["budget_allocated"]["amount"] == first["step"].reserve
    art = await complete(db, tid, first)
    second = await open_step(db, tid)
    assert second["inputs"][art["name"]] == {
        "version": 1,
        "content": {"rows": ["first"]},
    }
    async with db() as session:
        repo = WorkbenchRepository(session)
        a = await repo.get_attempt(second["attempt_id"])
        assert a["input_artifact_versions"][0]["id"] == art["id"]
        assert a["input_artifact_versions"][0]["digest"] == art["digest"]
        assert a["refs"]["tools"] == []


async def test_unaccepted_latest_artifact_cannot_replace_successful_input(db):
    tid, _ = await prepared(db)
    first = await open_step(db, tid)
    art = await complete(db, tid, first)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=None,
                name=art["name"],
                kind="step",
                content={"rows": ["unaccepted"]},
            )
    opened = await open_step(db, tid)
    assert opened["inputs"][art["name"]]["version"] == 1
    assert opened["inputs"][art["name"]]["content"]["rows"] == ["first"]


async def test_same_catalog_tasks_dispatch_different_instances_without_cache_pollution(
    db,
):
    a, _ = await prepared(db)
    b, _ = await prepared(db, plan=proposal(review=True))
    async with db() as session:
        repo = WorkbenchRepository(session)
        av = await execution_for_task(repo, await repo.get_task(a))
        bv = await execution_for_task(repo, await repo.get_task(b))
        assert len(av.pack.workflow) == 2 and len(bv.pack.workflow) == 3
        assert av.receipt.task_id == a and bv.receipt.task_id == b
        assert (await repo.get_task(a))["pack_snapshot"] == (await repo.get_task(b))[
            "pack_snapshot"
        ]


async def test_concurrent_dispatch_creates_only_one_attempt_and_reservation(db):
    tid, _ = await prepared(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        version = (await repo.get_task(tid))["state_version"]
    results = await asyncio.gather(
        open_step(db, tid, version), open_step(db, tid, version), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, StaleStateVersion) for r in results) == 1
    async with db() as session:
        repo = WorkbenchRepository(session)
        steps = [
            a for a in await repo.list_attempts(tid) if a["step_id"] == "inspect_1"
        ]
        assert len(steps) == 1
        assert (await repo.get_task(tid))["budget"]["reserved"] == steps[0][
            "budget_allocated"
        ]["amount"]


async def test_missing_plan_and_cancelled_tasks_cannot_dispatch(db):
    tid, _ = await task(db)
    with pytest.raises(MethodPlanInvalid, match="accepted"):
        await open_step(db, tid)
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    async with db() as session:
        await Ledger(WorkbenchRepository(session)).transition(tid, TaskState.CANCELLED)
    with pytest.raises(MethodPlanInvalid, match="idle"):
        await open_step(db, tid)


async def test_rewind_invalidates_suffix_and_stale_lineage(db):
    tid, _ = await prepared(db)
    await complete(db, tid, await open_step(db, tid))
    await complete(db, tid, await open_step(db, tid))
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            row = await repo.get_task(tid)
            await repo.cas_task(
                tid, expected_version=row["state_version"], current_step=0
            )
        row = await repo.get_task(tid)
        assert (
            await accepted_results(repo, row, await execution_for_task(repo, row)) == {}
        )
    await complete(db, tid, await open_step(db, tid), value="reworked")
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        results = await accepted_results(repo, row, await execution_for_task(repo, row))
        assert set(results) == {"inspect_1"}
        assert results["inspect_1"]["version"] == 2
        # A wrongly advanced cursor must not resurrect the old dependent result.
        with pytest.raises(MethodPlanInvalid, match="stale input lineage"):
            await accepted_results(
                repo, row | {"current_step": 2}, await execution_for_task(repo, row)
            )
    opened = await open_step(db, tid)
    assert opened["inputs"][output_name("inspect_1")]["version"] == 2


@pytest.mark.parametrize(
    "key,value", [("tools", ["compute"]), ("plan_version", 2), ("method_id", "other")]
)
async def test_active_attempt_permissions_fail_closed_on_binding_drift(db, key, value):
    tid, _ = await prepared(db)
    opened = await open_step(db, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            a = await repo.get_attempt(opened["attempt_id"])
            await repo.update_attempt(
                opened["attempt_id"], refs=a["refs"] | {key: value}
            )
        row = await repo.get_task(tid)
        with pytest.raises(MethodPlanInvalid, match="binding changed"):
            await active_method_step(repo, row, await execution_for_task(repo, row))


async def test_dispatch_event_failure_rolls_back_reservation_and_binding(
    db, monkeypatch
):
    tid, _ = await prepared(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        before = await repo.get_task(tid)

        async def fail(**kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(repo, "append_event", fail)
        with pytest.raises(RuntimeError, match="injected"):
            await Ledger(repo).open_method_step(
                tid, expected_version=before["state_version"]
            )
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["active_attempt_id"] is None
        assert row["budget"] == before["budget"]
        assert len(await repo.list_attempts(tid)) == 1


async def test_replan_and_dispatch_use_the_same_exact_successful_result(db):
    tid, _ = await prepared(db)
    art = await complete(db, tid, await open_step(db, tid))
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=None,
                name=art["name"],
                kind="step",
                content={"rows": ["not accepted"]},
            )
    aid, version = await planned_attempt(db, tid)
    raw = proposal(parent=1, review=True).model_dump(mode="json")
    for step in raw["steps"][1:]:
        step["inputs"][0]["version"] = art["version"]
    from app.domain.workbench.method_plan import MethodPlan

    await accept(db, tid, aid, version, MethodPlan.model_validate(raw))
    opened = await open_step(db, tid)
    assert opened["step"].step_id == "review_1"
    assert opened["inputs"][art["name"]]["version"] == 1
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert (
            await active_method_step(repo, row, await execution_for_task(repo, row))
        ).step_id == "review_1"
        assert (await repo.get_attempt(opened["attempt_id"]))["refs"][
            "plan_version"
        ] == 2


async def test_budget_refusal_does_not_open_method_attempt(db):
    from app.domain.workbench.errors import BudgetExhausted

    tid, _ = await prepared(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            row = await repo.get_task(tid)
            await repo.cas_task(
                tid,
                expected_version=row["state_version"],
                budget={
                    "currency": "CNY",
                    "limit": "0.02",
                    "used": "0.02",
                    "reserved": "0",
                },
            )
    with pytest.raises(BudgetExhausted):
        await open_step(db, tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert len(await repo.list_attempts(tid)) == 1
        assert (await repo.get_task(tid))["active_attempt_id"] is None


async def test_fixed_task_keeps_existing_pack_identity_and_has_no_dynamic_dispatch(db):
    from test_workbench_ledger_db import OWNER, handover, seed

    from app.application.workbench.catalog import pack_for_task

    _, _, sid = await seed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        tid = (
            await Ledger(repo).handover(
                handover(sid, key="fixed-view"), owner_actor_id=OWNER
            )
        )["task_id"]
        row = await repo.get_task(tid)
        view = await execution_for_task(repo, row)
        assert view.pack is pack_for_task(row)
        assert view.receipt is None
        with pytest.raises(MethodPlanInvalid):
            await Ledger(repo).open_method_step(
                tid, expected_version=row["state_version"]
            )


async def test_bound_version_cannot_silently_follow_a_reworked_output(db):
    from app.domain.workbench.method_plan import MethodPlan

    tid, _ = await prepared(db)
    await complete(db, tid, await open_step(db, tid))
    aid, version = await planned_attempt(db, tid)
    raw = proposal(parent=1).model_dump(mode="json")
    raw["steps"][1]["inputs"][0]["version"] = 1
    await accept(db, tid, aid, version, MethodPlan.model_validate(raw))
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            row = await repo.get_task(tid)
            await repo.cas_task(
                tid, expected_version=row["state_version"], current_step=0
            )
    await complete(db, tid, await open_step(db, tid), value="new upstream")
    with pytest.raises(MethodPlanInvalid, match="invalidated by rework"):
        await open_step(db, tid)


async def test_cancel_cannot_miss_a_step_opened_after_its_initial_read(db, monkeypatch):
    from test_workbench_ledger_db import OWNER

    tid, _ = await prepared(db)
    observed, dispatch_done = asyncio.Event(), asyncio.Event()

    async def cancel():
        async with db() as session:
            repo = WorkbenchRepository(session)
            original = repo.get_task
            intercepted = False

            async def get_task(task_id, *, owner_actor_id=None, for_update=False):
                nonlocal intercepted
                if not intercepted:
                    intercepted = True
                    # Reproduce the old unlocked peek. The new cancellation
                    # must reread under its requested task lock after dispatch.
                    old = await original(task_id, owner_actor_id=owner_actor_id)
                    assert old["active_attempt_id"] is None
                    observed.set()
                    await asyncio.wait_for(dispatch_done.wait(), timeout=5)
                    if not for_update:
                        return old
                return await original(
                    task_id, owner_actor_id=owner_actor_id, for_update=for_update
                )

            monkeypatch.setattr(repo, "get_task", get_task)
            return await Ledger(repo).request_cancel(tid, owner_actor_id=OWNER)

    async def dispatch():
        await asyncio.wait_for(observed.wait(), timeout=5)
        try:
            return await open_step(db, tid)
        finally:
            dispatch_done.set()

    cancelled, opened = await asyncio.wait_for(
        asyncio.gather(cancel(), dispatch()), timeout=10
    )
    assert cancelled["state"] == TaskState.CANCELLED
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_task(tid)
        assert row["active_attempt_id"] is None
        assert Decimal(row["budget"]["reserved"]) == 0
        assert (await repo.get_attempt(opened["attempt_id"]))[
            "status"
        ] == AttemptState.CANCELLED
        view = await execution_for_task(repo, row)
        with pytest.raises(MethodPlanInvalid, match="no active"):
            await active_method_step(repo, row, view)


async def test_caller_cannot_reuse_another_tasks_view_even_for_identical_plans(db):
    left, _ = await prepared(db)
    right, _ = await prepared(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        left_view = await execution_for_task(repo, await repo.get_task(left))
        right_task = await repo.get_task(right)
        with pytest.raises(MethodPlanInvalid, match="another task or plan"):
            await accepted_results(repo, right_task, left_view)
