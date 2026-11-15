"""Pinned step inputs, cumulative direct grants and concurrent host recovery."""

import asyncio
import json

import pytest
from formal_plan_support import advance, setup_plan
from sqlalchemy import text
from test_formal_execution_db import approve
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_execution import FormalExecution
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.method_authorization import (
    MethodAuthorization,
    check_delegation,
)
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)


async def second_step(db):
    ctx = await setup_plan(db)
    first = await advance(db, ctx)
    await approve(db, ctx.request, ctx.factory, first)
    second = await advance(db, ctx)
    return ctx, second, second["cases"][0]["task_id"]


async def test_newer_unaccepted_artifact_cannot_replace_pinned_input(db):
    ctx, pending, tid = await second_step(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        async with repo.transaction():
            await repo.put_artifact(
                task_id=tid,
                attempt_id=str(task["active_attempt_id"]),
                name="formal-step:first",
                kind="step",
                content={"receipt": "unauthorized@example.test"},
            )
    await approve(db, ctx.request, ctx.factory, pending)
    result = await advance(db, ctx)
    assert result["status"] == "passed", result
    assert ctx.remote.calls[-1][3] == {"to": "test@example.test"}


@pytest.mark.parametrize(
    "mutation", ["artifact_content", "input_version", "parent_revoked"]
)
async def test_approval_rebuilds_pinned_inputs_and_current_parent_authority(
    db, mutation
):
    ctx, pending, tid = await second_step(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        async with repo.transaction():
            if mutation == "artifact_content":
                await session.execute(
                    text(
                        "UPDATE workbench_artifacts SET content=cast(:value AS jsonb) WHERE task_id=:t AND name='formal-step:first'"
                    ),
                    {
                        "t": tid,
                        "value": json.dumps({"receipt": "changed@example.test"}),
                    },
                )
            elif mutation == "input_version":
                attempt = await repo.get_attempt(str(task["active_attempt_id"]))
                refs = attempt["refs"]
                refs["input_bindings"][0]["version"] += 1
                await repo.update_attempt(str(attempt["id"]), refs=refs)
            else:
                root = await repo.call_by_key(
                    ctx.owner, str(task["session_id"]), "task:" + tid
                )
                assert root is not None
                authority = await repo.method_authorization(
                    ctx.owner, root["id"], lock=True
                )
                assert authority is not None
                previous = authority["revision"]
                authority.update(revision=previous + 1, revoked=True)
                await repo.save_method_authorization(
                    ctx.owner, str(task["session_id"]), authority, previous=previous
                )
    with pytest.raises(CapabilityError):
        await approve(db, ctx.request, ctx.factory, pending)
    assert len(ctx.remote.calls) == 1


async def test_parallel_drivers_keep_one_call_per_slot_and_cumulative_exact_grants(db):
    ctx = await setup_plan(db)
    first = await advance(db, ctx)
    tid = first["cases"][0]["task_id"]
    await approve(db, ctx.request, ctx.factory, first)

    async def drive():
        return await FormalExecution(SqlWorkbenchStores(db), ctx.factory).drive(tid)

    results = await asyncio.wait_for(
        asyncio.gather(drive(), drive(), return_exceptions=True), 15
    )
    assert not [r for r in results if isinstance(r, BaseException)], results
    second = await advance(db, ctx)
    assert len(second["actions"]) == 1 and len(ctx.remote.calls) == 1
    await approve(db, ctx.request, ctx.factory, second)
    results = await asyncio.wait_for(
        asyncio.gather(drive(), drive(), return_exceptions=True), 15
    )
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert (await advance(db, ctx))["status"] == "passed"
    assert len(ctx.remote.calls) == 2
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        root = await repo.call_by_key(ctx.owner, str(task["session_id"]), "task:" + tid)
        assert root is not None
        parent = MethodAuthorization.model_validate(
            await repo.method_authorization(ctx.owner, root["id"])
        )
        assert len(parent.grants) == 2 and parent.revision == 2
        for attempt in await repo.list_attempts(tid):
            child = MethodAuthorization.model_validate(
                await repo.method_authorization(
                    ctx.owner, attempt["refs"]["method_call_id"]
                )
            )
            check_delegation(parent, child)
            assert len(child.grants) == 1
            assert not child.grants[0].delegable
            assert child.grants[0].recipient_call_id == child.call_id
            assert child.grants[0].resource_authority is None
