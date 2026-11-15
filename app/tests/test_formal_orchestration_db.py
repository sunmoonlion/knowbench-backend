"""Actual expert Task/Attempt/approval/dispatch/evidence; no model or real supplier."""

import pytest
from formal_plan_support import advance, freeze, setup_plan
from sqlalchemy import text
from test_formal_execution_db import approve
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_two_real_steps_are_approved_independently_and_seal_expert_after_restart(
    db,
):
    ctx = await setup_plan(db)
    first = await advance(db, ctx)
    assert len(first["actions"]) == 1 and not ctx.remote.calls
    first_call = first["actions"][0]["call_id"]
    tid = first["cases"][0]["task_id"]
    await approve(db, ctx.request, ctx.factory, first)
    ctx.conductor = KnowbotTestConductor(
        workbench_stores(db), formal_capabilities=ctx.factory
    )
    second = await advance(db, ctx)
    assert len(second["actions"]) == 1 and len(ctx.remote.calls) == 1
    assert second["actions"][0]["call_id"] != first_call
    # An old approval cannot dispatch this new step.
    await approve(db, ctx.request, ctx.factory, first)
    assert len(ctx.remote.calls) == 1
    await approve(db, ctx.request, ctx.factory, second)
    final = await advance(db, ctx)
    assert final["status"] == "passed", final
    assert len(ctx.remote.calls) == 2
    assert [call[3] for call in ctx.remote.calls] == [{"to": "test@example.test"}] * 2
    expert = freeze(ctx.request, final)
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        attempts = await repo.list_attempts(tid)
        assert task["state"] == "SUCCEEDED" and len(attempts) == 2
        assert all(a["turn_ids"] == [] for a in attempts)
        assert [a["refs"]["slot"] for a in attempts] == ["first", "second"]
        assert len({a["refs"]["method_call_id"] for a in attempts}) == 2
        root = await repo.call_by_key(ctx.owner, str(task["session_id"]), "task:" + tid)
        assert root is not None
        for attempt in attempts:
            child = await repo.call_by_key(
                ctx.owner,
                str(task["session_id"]),
                "formal-method-step:" + tid + ":" + attempt["refs"]["slot"],
            )
            assert (
                child is not None and child["id"] == attempt["refs"]["method_call_id"]
            )
            tool = await repo.capability_call(
                ctx.owner, attempt["refs"]["capability_call_id"]
            )
            assert child["parent_call_id"] == root["id"]
            assert tool["parent_call_id"] == child["id"]
        validation = await repo.formal_validation(ctx.owner, str(ctx.request.run_id))
        assert validation is not None
        evidence = validation["execution_evidence"]
        assert evidence["adapter"] == "knowbench/formal-orchestration-host/v1"
        assert evidence["cases"][0]["validated"]
        assert len(evidence["cases"][0]["steps"]) == 2
        await ctx.factory(repo).formal_sources.register(expert, caller="knowlab")
        async with repo.transaction():
            await repo.purge_test_run(str(ctx.request.run_id))
        # Registration and retained proof survive task cleanup, but ordinary
        # conversation use of an expert is deliberately not enabled here.
        await ctx.factory(repo).formal_sources.register(expert, caller="knowlab")
        with pytest.raises((CapabilityError, ValueError)):
            await FormalMethodCatalog(
                repo, ctx.factory(repo).formal_sources, ctx.factory(repo).operations
            ).resolve_owned(ctx.owner, expert.reference)


@pytest.mark.parametrize(
    "failure", ["decline", "unknown", "invalid_output", "source_changed", "cancel"]
)
async def test_second_step_failure_never_replays_first_or_qualifies_expert(db, failure):
    ctx = await setup_plan(db)
    first = await advance(db, ctx)
    await approve(db, ctx.request, ctx.factory, first)
    second = await advance(db, ctx)
    assert len(ctx.remote.calls) == 1
    if failure == "unknown":
        ctx.remote.fail = True
    if failure == "invalid_output":
        ctx.remote.invalid = True
    if failure == "source_changed":
        async with db() as session:
            async with WorkbenchRepository(session).transaction():
                await session.execute(
                    text(
                        "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                    ),
                    {"id": ctx.asset["id"]},
                )
        with pytest.raises(CapabilityError):
            await approve(db, ctx.request, ctx.factory, second)
    elif failure == "cancel":
        async with db() as session:
            repo = WorkbenchRepository(session)
            await FormalExecutions(repo, ctx.factory(repo)).cancel(
                "author", str(ctx.request.run_id), caller="knowlab"
            )
    else:
        await approve(
            db,
            ctx.request,
            ctx.factory,
            second,
            decision="decline" if failure == "decline" else "accept",
        )
    result = await advance(db, ctx)
    assert result["status"] in ("failed", "cancelled"), result
    assert result["admission"]["receipt"] is None
    calls = len(ctx.remote.calls)
    for _ in range(3):
        await ctx.conductor.advance(str(ctx.request.run_id))
    assert len(ctx.remote.calls) == calls
    assert calls == (2 if failure in ("unknown", "invalid_output") else 1)


async def test_first_expert_registration_rechecks_current_leaf_source(db):
    ctx = await setup_plan(db)
    for _ in range(2):
        pending = await advance(db, ctx)
        await approve(db, ctx.request, ctx.factory, pending)
    final = await advance(db, ctx)
    assert final["status"] == "passed", final
    expert = freeze(ctx.request, final)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                ),
                {"id": ctx.asset["id"]},
            )
        with pytest.raises(CapabilityError):
            await ctx.factory(repo).formal_sources.register(expert, caller="knowlab")
        assert (
            await repo.formal_method(ctx.owner, str(expert.reference.method_id), 1)
            is None
        )


@pytest.mark.parametrize(
    "change", ["legacy_receipt", "source_revision", "missing_version", "parent_scope"]
)
async def test_plan_intake_requires_current_real_host_qualified_leaves(db, change):
    import json

    from app.domain.workbench.formal_method_execution_generated import (
        FormalMethodExecutionRequest,
    )

    ctx = await setup_plan(db, accept=False)
    document = ctx.request.model_dump(mode="json", by_alias=True)
    if change == "missing_version":
        for step in document["draft"]["definition"]["procedure"]["steps"]:
            step["candidates"][0]["version"] = 2
        document["dependencies"][0]["version"] = 2
    elif change == "parent_scope":
        document["draft"]["definition"]["access_requirements"][0]["resources"][0][
            "values"
        ] = ["another@example.test"]
    async with db() as session:
        repo = WorkbenchRepository(session)
        if change in ("legacy_receipt", "source_revision"):
            async with repo.transaction():
                if change == "source_revision":
                    await session.execute(
                        text(
                            "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                        ),
                        {"id": ctx.asset["id"]},
                    )
                else:
                    frozen = ctx.leaf.model_dump(mode="json", by_alias=True)
                    frozen["validation"]["execution"] = None
                    await session.execute(
                        text(
                            "UPDATE workbench_formal_methods SET document=cast(:d as jsonb) WHERE method_id=:id AND version=1"
                        ),
                        {
                            "d": json.dumps(frozen),
                            "id": str(ctx.leaf.reference.method_id),
                        },
                    )
        with pytest.raises(CapabilityError):
            await FormalExecutions(repo, ctx.factory(repo)).accept(
                FormalMethodExecutionRequest.model_validate(document), caller="knowlab"
            )
        assert await repo.get_test_run(str(ctx.request.run_id)) is None


@pytest.mark.parametrize(
    "seconds,expected,expired", [(0.9, 1, False), (30.9, 61, True)]
)
async def test_approval_wait_is_summed_before_rounding_and_enforces_total_limit(
    db, seconds, expected, expired
):
    from datetime import UTC, datetime, timedelta

    ctx = await setup_plan(db, accept=False)
    ctx.request = ctx.request.model_copy(
        update={
            "execution": ctx.request.execution.model_copy(
                update={"max_wait_seconds": 60, "timeout_seconds": 600}
            )
        }
    )
    async with db() as session:
        repo = WorkbenchRepository(session)
        await FormalExecutions(repo, ctx.factory(repo)).accept(
            ctx.request, caller="knowlab"
        )
    first = await advance(db, ctx)
    await approve(db, ctx.request, ctx.factory, first)
    second = await advance(db, ctx)
    await approve(db, ctx.request, ctx.factory, second)
    now = datetime.now(UTC)
    start = now - timedelta(seconds=75)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            for i, view in enumerate((first, second)):
                action = view["actions"][0]
                created = start + timedelta(seconds=i * 32)
                await session.execute(
                    text(
                        "UPDATE workbench_interactions SET created_at=:c,consumed_at=:u WHERE id=:id"
                    ),
                    {
                        "c": created,
                        "u": created + timedelta(seconds=seconds),
                        "id": action["interaction_id"],
                    },
                )
            await session.execute(
                text(
                    "UPDATE workbench_test_runs SET created_at=:t,started_at=:t,waited_seconds=0 WHERE run_id=:id"
                ),
                {"t": start, "id": str(ctx.request.run_id)},
            )
            await session.execute(
                text(
                    "UPDATE workbench_test_cases SET waited_seconds=0 WHERE run_id=:id"
                ),
                {"id": str(ctx.request.run_id)},
            )
    ctx.conductor.now = lambda: now
    await ctx.conductor.advance(str(ctx.request.run_id))
    async with db() as session:
        row = await WorkbenchRepository(session).get_test_run(str(ctx.request.run_id))
        assert row["waited_seconds"] == expected
        assert (row["status"] == "failed") is expired
    if not expired:
        await ctx.conductor.advance(str(ctx.request.run_id))
        async with db() as session:
            assert (
                await WorkbenchRepository(session).get_test_run(str(ctx.request.run_id))
            )["waited_seconds"] == expected
