"""Real qualified nested methods, direct owner approval, restart and failed subtrees."""

import json

import pytest
from formal_plan_support import advance, freeze
from formal_recursive_support import accept_request, assert_passed, setup_recursive
from sqlalchemy import text
from test_formal_execution_db import approve
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_orchestration import resolve_plan
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_execution import expanded_plan, snapshot_for_task
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize(
    "variant,kind",
    [
        ("expert", "expert"),
        ("versions", "expert"),
        ("team", "expert_team"),
        ("team_parent", "expert_team"),
    ],
)
async def test_nested_qualified_methods_preserve_each_call_and_private_registration(
    db, variant, kind
):
    ctx = await setup_recursive(db, variant=variant)
    old = None
    for index in range(4):
        pending = await advance(db, ctx)
        assert len(pending["actions"]) == 1, pending
        assert len(ctx.remote.calls) == index
        if old:
            await approve(db, ctx.request, ctx.factory, old)
            assert len(ctx.remote.calls) == index
        await approve(db, ctx.request, ctx.factory, pending)
        old = pending
        ctx.conductor = KnowbotTestConductor(
            workbench_stores(db), formal_capabilities=ctx.factory
        )
    final = await advance(db, ctx)
    await assert_passed(db, ctx, final)
    assert len(ctx.remote.calls) == 4
    assert [call[3] for call in ctx.remote.calls] == [{"to": "test@example.test"}] * 4
    tid = final["cases"][0]["task_id"]
    frozen = freeze(ctx.request, final)
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        snapshot = snapshot_for_task(task)
        plan = expanded_plan(snapshot)
        assert plan.method_kind == kind
        assert len(plan.leaf_nodes) == 4
        attempts = await repo.list_attempts(tid)
        assert len(attempts) == 4 and all(a["turn_ids"] == [] for a in attempts)
        calls = await repo.list_task_calls(ctx.owner, tid)
        methods = [c for c in calls if c["kind"] == "method"]
        tools = [c for c in calls if c["kind"] == "tool"]
        assert len(methods) == len(plan.nodes) + 1 and len(tools) == 4
        assert all(c["status"] == "succeeded" for c in calls)
        root = next(c for c in methods if c["parent_call_id"] is None)
        by_id = {c["id"]: c for c in methods}
        for tool in tools:
            leaf = by_id[tool["parent_call_id"]]
            assert leaf["parent_call_id"] != root["id"]
            auth = await repo.method_authorization(ctx.owner, leaf["id"])
            assert auth is not None
            assert auth["parent_call_id"] == root["id"]
            assert len(auth["grants"]) == 1
            assert not auth["grants"][0]["delegable"]
            assert auth["grants"][0]["recipient_call_id"] == leaf["id"]
        await ctx.factory(repo).formal_sources.register(frozen, caller="knowlab")
        async with repo.transaction():
            await repo.purge_test_run(str(ctx.request.run_id))
        retained = {
            (f.reference.method_id, f.reference.version): f
            for f in snapshot.dependencies
        }
        assert (
            await FormalMethodCatalog(
                repo, ctx.factory(repo).formal_sources, ctx.factory(repo).operations
            ).resolve_owned_admission(ctx.owner, frozen.reference, resolved=retained)
            == frozen
        )
        # Real recursive qualification still does not open ordinary expert use.
        with pytest.raises((CapabilityError, ValueError)):
            await FormalMethodCatalog(
                repo, ctx.factory(repo).formal_sources, ctx.factory(repo).operations
            ).resolve_owned(ctx.owner, frozen.reference)


@pytest.mark.parametrize(
    "limit,value", [("max_depth", 2), ("max_expansions", 6), ("max_steps", 5)]
)
async def test_recursive_intake_rejects_whole_subtree_limits_before_action(
    db, limit, value
):
    ctx = await setup_recursive(db, accept=False)
    document = ctx.request.model_dump(mode="json", by_alias=True)
    document["draft"]["definition"]["procedure"][limit] = value
    ctx.request = FormalMethodExecutionRequest.model_validate(document)
    with pytest.raises(CapabilityError):
        await accept_request(db, ctx)
    assert ctx.remote.calls == []


@pytest.mark.parametrize(
    "failure",
    [
        "decline",
        "unknown",
        "invalid_output",
        "source_changed",
        "cancel",
        "parent_revoked",
    ],
)
async def test_later_subtree_failure_preserves_completed_prefix_and_never_replays(
    db, failure
):
    ctx = await setup_recursive(db)
    for _ in range(2):
        await approve(db, ctx.request, ctx.factory, await advance(db, ctx))
    pending = await advance(db, ctx)
    assert len(ctx.remote.calls) == 2 and pending["actions"]
    tid = pending["cases"][0]["task_id"]
    if failure == "unknown":
        ctx.remote.fail = True
    if failure == "invalid_output":
        ctx.remote.invalid = True
    if failure in ("source_changed", "parent_revoked", "cancel"):
        async with db() as session:
            repo = WorkbenchRepository(session)
            if failure == "cancel":
                await FormalExecutions(repo, ctx.factory(repo)).cancel(
                    "author", str(ctx.request.run_id), caller="knowlab"
                )
            elif failure == "source_changed":
                async with repo.transaction():
                    await session.execute(
                        text(
                            "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                        ),
                        {"id": ctx.asset["id"]},
                    )
            else:
                task = await repo.get_task(tid)
                attempt = await repo.get_attempt(str(task["active_attempt_id"]))
                parent_id = attempt["refs"]["parent_method_call_id"]
                async with repo.transaction():
                    auth = await repo.method_authorization(
                        ctx.owner, parent_id, lock=True
                    )
                    assert auth is not None
                    previous = auth["revision"]
                    auth.update(revision=previous + 1, revoked=True)
                    await repo.save_method_authorization(
                        ctx.owner, str(task["session_id"]), auth, previous=previous
                    )
        if failure != "cancel":
            with pytest.raises(CapabilityError):
                await approve(db, ctx.request, ctx.factory, pending)
    else:
        await approve(
            db,
            ctx.request,
            ctx.factory,
            pending,
            decision="decline" if failure == "decline" else "accept",
        )
    final = await advance(db, ctx)
    assert final["status"] in ("failed", "cancelled"), final
    assert final["admission"]["receipt"] is None
    assert len(ctx.remote.calls) == (
        3 if failure in ("unknown", "invalid_output") else 2
    )
    before = len(ctx.remote.calls)
    for _ in range(3):
        await ctx.conductor.advance(str(ctx.request.run_id))
    assert len(ctx.remote.calls) == before
    async with db() as session:
        repo = WorkbenchRepository(session)
        calls = await repo.list_task_calls(ctx.owner, tid)
        methods = [c for c in calls if c["kind"] == "method"]
        # The completed first expert remains successful even when its sibling fails.
        assert sum(c["status"] == "succeeded" for c in methods) >= 3
        assert not [
            c for c in methods if c["status"] in ("created", "running", "waiting")
        ]


async def test_recursive_intake_resolves_real_expert_after_its_tasks_are_cleaned(db):
    ctx = await setup_recursive(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        pins = await resolve_plan(
            ctx.factory(repo), ctx.owner, ctx.request.draft, ctx.request.method_id
        )
        assert {f.reference.digest for f in pins} == {
            ctx.leaf.reference.digest,
            ctx.experts[0].reference.digest,
        }
        run = await repo.get_test_run(str(ctx.request.run_id))
        assert run is not None and run["status"] == "accepted"
    assert ctx.remote.calls == []


@pytest.mark.parametrize(
    "change", ["expert_evidence", "source_revision", "public_expert"]
)
async def test_recursive_intake_requires_private_retained_expert_and_current_leaf(
    db, change
):
    ctx = await setup_recursive(db, accept=False)
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            if change == "expert_evidence":
                await session.execute(
                    text(
                        "UPDATE workbench_formal_validations SET execution_evidence='{}'::jsonb WHERE run_id=:id"
                    ),
                    {"id": str(ctx.experts[0].validation.run_id)},
                )
            elif change == "source_revision":
                await session.execute(
                    text(
                        "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                    ),
                    {"id": ctx.asset["id"]},
                )
            else:
                # Make the frozen record structurally public-qualified so this
                # exercises private ownership policy, not a malformed schema.
                document = ctx.experts[0].model_dump(mode="json", by_alias=True)
                document["visibility"] = "public"
                document["validation"]["uncovered"] = []
                document["validation"]["checks"].append("business")
                await session.execute(
                    text(
                        "UPDATE workbench_formal_methods SET document=cast(:document AS jsonb) WHERE method_id=:id"
                    ),
                    {
                        "id": str(ctx.experts[0].reference.method_id),
                        "document": json.dumps(document),
                    },
                )
    with pytest.raises(CapabilityError):
        await accept_request(db, ctx)
    assert ctx.remote.calls == []
