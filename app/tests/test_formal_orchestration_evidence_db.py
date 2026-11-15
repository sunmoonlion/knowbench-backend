"""Mutate actual completed plan facts before sealing; no fabricated success proof."""

import json

import pytest
from formal_plan_support import advance, setup_plan
from sqlalchemy import text
from test_formal_execution_db import approve, status
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_orchestration_evidence import (
    collect_plan_evidence,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


async def completed_unsealed(db):
    ctx = await setup_plan(db)
    await approve(db, ctx.request, ctx.factory, await advance(db, ctx))
    await approve(db, ctx.request, ctx.factory, await advance(db, ctx))
    # Drive the last accepted action, then separately record its case. The next
    # conductor pass seals the run, leaving a precise mutation window here.
    await ctx.conductor.advance(str(ctx.request.run_id))
    await ctx.conductor.advance(str(ctx.request.run_id))
    async with db() as session:
        repo = WorkbenchRepository(session)
        run = await repo.get_test_run(str(ctx.request.run_id))
        assert run is not None
        rows = await repo.list_test_cases(str(ctx.request.run_id))
        assert run["status"] == "running"
        assert len(rows) == 1 and rows[0]["result"]["outcome"] == "succeeded"
        result = rows[0]["result"]
        evidence = await collect_plan_evidence(
            repo, ctx.request, ctx.request.execution.suite.cases[0], 1, result
        )
        assert evidence["validated"], evidence["errors"]
        assert len(evidence["steps"]) == 2
        assert all(step["governance"]["approved"] for step in evidence["steps"])
        ctx.result = result
    return ctx


@pytest.mark.parametrize(
    "change",
    [
        "output",
        "input_binding",
        "authorization_event",
        "child_parent",
        "missing_required_result",
        "exports",
    ],
)
async def test_sealing_rejects_tampered_persisted_plan_facts(db, change):
    ctx = await completed_unsealed(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        attempts = await repo.list_attempts(ctx.result["task_id"])
        first, last = attempts
        async with repo.transaction():
            if change == "output":
                await session.execute(
                    text(
                        "UPDATE workbench_artifacts SET content=cast(:value as jsonb) WHERE id=:id"
                    ),
                    {
                        "id": first["output_artifacts"][0]["id"],
                        "value": json.dumps({"receipt": "changed@example.test"}),
                    },
                )
            elif change == "authorization_event":
                events = await repo.list_record_events(
                    session_id=str(first["session_id"]),
                    types=("method/permissionsConfirmed",),
                )
                event = next(
                    e for e in events if str(e["attempt_id"]) == str(first["id"])
                )
                payload = event["payload"]
                payload["parent_authorization"]["grants"] = []
                await session.execute(
                    text(
                        "UPDATE workbench_session_events SET payload=cast(:value as jsonb) WHERE id=:id"
                    ),
                    {"id": event["id"], "value": json.dumps(payload)},
                )
            elif change == "child_parent":
                await session.execute(
                    text(
                        "UPDATE workbench_calls SET parent_call_id=:parent WHERE id=:id"
                    ),
                    {
                        "id": last["refs"]["method_call_id"],
                        "parent": first["refs"]["method_call_id"],
                    },
                )
            else:
                target = first if change == "missing_required_result" else last
                refs = target["refs"]
                if change == "input_binding":
                    refs["input_bindings"][0]["digest"] = "f" * 64
                elif change == "missing_required_result":
                    refs.pop("formal_result")
                else:
                    refs["formal_exports"]["outputs"]["final_receipt"] = (
                        "changed@example.test"
                    )
                await repo.update_attempt(str(target["id"]), refs=refs)
        evidence = await collect_plan_evidence(
            repo, ctx.request, ctx.request.execution.suite.cases[0], 1, ctx.result
        )
        assert not evidence["validated"] and evidence["errors"]
    await ctx.conductor.advance(str(ctx.request.run_id))
    final = await status(db, ctx.request, ctx.factory)
    assert final["status"] == "failed", final
    assert final["admission"]["receipt"] is None
    assert len(ctx.remote.calls) == 2
