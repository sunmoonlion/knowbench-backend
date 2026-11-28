"""Tamper real completed recursive facts before sealing, never fabricate a pass."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from formal_recursive_support import completed_unsealed, setup_recursive
from sqlalchemy import text
from test_formal_execution_db import status
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_recursive_evidence import (
    collect_recursive_evidence,
)
from app.domain.workbench.capabilities import digest
from app.domain.workbench.formal_recursive import TREE_ADAPTER
from app.infrastructure.workbench.repository import WorkbenchRepository


async def collected(db, ctx):
    async with db() as session:
        return await collect_recursive_evidence(
            WorkbenchRepository(session),
            ctx.request,
            ctx.request.execution.suite.cases[0],
            1,
            ctx.result,
        )


async def finished(db):
    ctx = await setup_recursive(db)
    await completed_unsealed(db, ctx)
    evidence = await collected(db, ctx)
    assert evidence["validated"], evidence["errors"]
    assert evidence["adapter"] == TREE_ADAPTER
    assert len(evidence["nodes"]) == 6 and len(evidence["steps"]) == 4
    leaves = [node for node in evidence["nodes"] if node["kind"] == "tool"]
    assert all(set(node) == {"kind", "path", "step_index"} for node in leaves)
    assert [evidence["steps"][node["step_index"]]["path"] for node in leaves] == [
        node["path"] for node in leaves
    ]

    assert all(step["governance"]["approved"] for step in evidence["steps"])
    assert len(ctx.remote.calls) == 4
    return ctx, evidence


async def update_event(session, event):
    await session.execute(
        text(
            "UPDATE workbench_session_events SET payload=cast(:payload as jsonb) WHERE id=:id"
        ),
        {"id": event["id"], "payload": json.dumps(event["payload"])},
    )


@pytest.mark.parametrize(
    "change",
    [
        "real_parent",
        "path",
        "bound_arguments",
        "input_artifact",
        "intermediate_exports",
        "intermediate_artifact",
        "intermediate_attempt",
        "leaf_attempt",
        "leaf_typed_output",
        "root_exports",
        "scope_creation",
        "scope_approval",
        "scope_expiry",
        "future_parent_revision",
        "direct_authority_parent",
        "root_direct_grant",
        "missing_leaf",
        "extra_call",
        "extra_artifact",
        "model_usage",
        "cost",
    ],
)
async def test_recursive_seal_rejects_independent_fact_tampering(db, change):
    ctx, evidence = await finished(db)
    root_id = evidence["root_call_id"]
    first = evidence["steps"][0]
    nested = next(n for n in evidence["nodes"] if n["kind"] != "tool")
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            if change in {"real_parent", "intermediate_attempt"}:
                if change == "real_parent":
                    await session.execute(
                        text(
                            "UPDATE workbench_calls SET parent_call_id=:parent WHERE id=:id"
                        ),
                        {"id": first["method_call_id"], "parent": root_id},
                    )
                else:
                    await session.execute(
                        text(
                            "UPDATE workbench_artifacts SET attempt_id=:attempt WHERE id=:id"
                        ),
                        {
                            "id": nested["artifact"]["id"],
                            "attempt": first["attempt_id"],
                        },
                    )
            elif change in {
                "path",
                "bound_arguments",
                "input_artifact",
                "intermediate_exports",
            }:
                target = nested if change == "intermediate_exports" else first
                if change == "input_artifact":
                    target = next(s for s in evidence["steps"] if s["input_bindings"])
                call = await repo.call_by_key(
                    ctx.owner, target["call"]["session_id"], target["call"]["call_key"]
                )
                assert call is not None
                detail = call["detail"]
                if change == "path":
                    detail["path"] = ["unplanned", "first"]
                elif change == "bound_arguments":
                    detail["arguments"] = {"to": "changed@example.test"}
                    detail["arguments_digest"] = digest(detail["arguments"])
                elif change == "input_artifact":
                    detail["input_bindings"][0]["method_call_id"] = (
                        first["method_call_id"]
                        if target["path"][0] != first["path"][0]
                        else root_id
                    )
                else:
                    detail["exports"]["outputs"] = {
                        "final_receipt": "changed@example.test"
                    }
                    detail["exports"]["outputs_digest"] = digest(
                        detail["exports"]["outputs"]
                    )
                await repo.update_call(ctx.owner, call["id"], call["status"], detail)
            elif change in {"intermediate_artifact", "root_exports"}:
                ref = (
                    nested["exports_artifact"]
                    if change == "intermediate_artifact"
                    else evidence["root_exports_artifact"]
                )
                await session.execute(
                    text(
                        "UPDATE workbench_artifacts SET content=cast(:content as jsonb) WHERE id=:id"
                    ),
                    {"id": ref["id"], "content": json.dumps({"forged": True})},
                )
            elif change in {"leaf_attempt", "missing_leaf"}:
                attempt = await repo.get_attempt(first["attempt_id"])
                if change == "leaf_attempt":
                    refs = attempt["refs"]
                    refs["parent_method_call_id"] = root_id
                    await repo.update_attempt(first["attempt_id"], refs=refs)
                else:
                    await repo.update_attempt(first["attempt_id"], status="FAILED")
            elif change == "leaf_typed_output":
                call = await repo.capability_call(ctx.owner, first["call_id"])
                detail = call["detail"]
                detail["result"]["structuredContent"] = {
                    "receipt": "changed@example.test"
                }
                await repo.update_call(ctx.owner, call["id"], call["status"], detail)
            elif change in {
                "scope_creation",
                "scope_approval",
                "scope_expiry",
                "root_direct_grant",
            }:
                events = await repo.list_record_events(
                    session_id=first["call"]["session_id"],
                    types=("method/scopeCreated", "method/permissionsConfirmed"),
                )
                if change == "scope_creation":
                    event = next(
                        e
                        for e in events
                        if e["type"] == "method/scopeCreated"
                        and e["payload"]["authorization"]["call_id"]
                        == nested["method_call_id"]
                    )
                    event["payload"]["path"] = ["another_scope"]
                else:
                    event = next(
                        e
                        for e in events
                        if e["type"] == "method/permissionsConfirmed"
                        and str(e["attempt_id"]) == first["attempt_id"]
                    )
                    if change == "scope_approval":
                        event["payload"]["scope_authorizations"][1]["revoked"] = True
                    elif change == "scope_expiry":
                        event["payload"]["scope_authorizations"][1]["expires_at"] = (
                            first["governance"]["approval"]["consumed_at"]
                        )
                    else:
                        event["payload"]["parent_authorization"]["grants"] = []
                await update_event(session, event)
            elif change == "future_parent_revision":
                # Keep every stored copy consistent while forging a parent
                # revision that only becomes real after this scope was created.
                current = await repo.method_authorization(
                    ctx.owner, nested["method_call_id"]
                )
                root = await repo.method_authorization(ctx.owner, root_id)
                assert current is not None and root is not None
                assert root["revision"] > current["parent_revision"]
                current["parent_revision"] = root["revision"]
                await session.execute(
                    text(
                        "UPDATE workbench_method_authorizations SET document=cast(:document as jsonb) WHERE call_id=:id"
                    ),
                    {"id": nested["method_call_id"], "document": json.dumps(current)},
                )
                events = await repo.list_record_events(
                    session_id=first["call"]["session_id"],
                    types=("method/scopeCreated", "method/permissionsConfirmed"),
                )
                for event in events:
                    payload = event["payload"]
                    if (
                        event["type"] == "method/scopeCreated"
                        and payload["authorization"]["call_id"]
                        == nested["method_call_id"]
                    ):
                        payload["authorization"]["parent_revision"] = root["revision"]
                    for scope in payload.get("scope_authorizations", []):
                        if scope["call_id"] == nested["method_call_id"]:
                            scope["parent_revision"] = root["revision"]
                    await update_event(session, event)
            elif change == "direct_authority_parent":
                authorization = await repo.method_authorization(
                    ctx.owner, first["method_call_id"]
                )
                assert authorization is not None
                authorization["parent_call_id"] = nested["method_call_id"]
                await session.execute(
                    text(
                        "UPDATE workbench_method_authorizations SET document=cast(:document as jsonb) WHERE call_id=:id"
                    ),
                    {
                        "id": first["method_call_id"],
                        "document": json.dumps(authorization),
                    },
                )
            elif change == "extra_call":
                await repo.add_call(
                    {
                        "id": str(uuid4()),
                        "owner_actor_id": ctx.owner,
                        "session_id": first["call"]["session_id"],
                        "task_id": ctx.result["task_id"],
                        "parent_call_id": root_id,
                        "kind": "method",
                        "origin": "method",
                        "call_key": "unexpected-extra-call",
                        "digest": digest(["extra"]),
                        "status": "succeeded",
                        "detail": {},
                    }
                )
            elif change == "extra_artifact":
                await repo.put_artifact(
                    task_id=ctx.result["task_id"],
                    attempt_id=None,
                    name="unexpected-extra-artifact",
                    kind="step",
                    content={},
                )
            elif change == "model_usage":
                await repo.append_event(
                    session_id=first["call"]["session_id"],
                    task_id=ctx.result["task_id"],
                    kind="task",
                    event_type="thread/tokenUsage/updated",
                    payload={"total": 1},
                )
            else:
                task = await repo.get_task(ctx.result["task_id"])
                await session.execute(
                    text(
                        "UPDATE workbench_tasks SET budget=cast(:budget as jsonb) WHERE id=:id"
                    ),
                    {
                        "id": ctx.result["task_id"],
                        "budget": json.dumps({**task["budget"], "used": "0.01"}),
                    },
                )
    changed = await collected(db, ctx)
    assert not changed["validated"] and changed["errors"], change
    if change == "future_parent_revision":
        assert changed["errors"] == ["nodes:scope_parent_revision_not_yet_effective"]
    await ctx.conductor.advance(str(ctx.request.run_id))
    final = await status(db, ctx.request, ctx.factory)
    assert final["status"] == "failed" and final["admission"]["receipt"] is None, final
    assert len(ctx.remote.calls) == 4


@pytest.mark.parametrize("before_approval", [False, True])
async def test_scope_revocation_is_evaluated_at_action_time(db, before_approval):
    ctx, evidence = await finished(db)
    scope = next(n for n in evidence["nodes"] if n["kind"] != "tool")
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            current = await repo.method_authorization(
                ctx.owner, scope["method_call_id"]
            )
            assert current is not None
            revision = current["revision"]
            current.update(revoked=True, revision=revision + 1)
            await repo.save_method_authorization(
                ctx.owner, scope["call"]["session_id"], current, previous=revision
            )
            event = await repo.append_event(
                session_id=scope["call"]["session_id"],
                task_id=ctx.result["task_id"],
                kind="task",
                event_type="method/permissionsRevoked",
                payload={
                    "call_id": scope["method_call_id"],
                    "revision": current["revision"],
                    "reason": "test withdrawal",
                },
            )
            if before_approval:
                first = await repo.get_attempt(evidence["steps"][0]["attempt_id"])
                await session.execute(
                    text(
                        "UPDATE workbench_session_events SET created_at=:at WHERE id=:id"
                    ),
                    {
                        "id": event["id"],
                        "at": first["created_at"] - timedelta(seconds=1),
                    },
                )
    observed = await collected(db, ctx)
    assert observed["validated"] is (not before_approval), observed["errors"]
    assert len(ctx.remote.calls) == 4
