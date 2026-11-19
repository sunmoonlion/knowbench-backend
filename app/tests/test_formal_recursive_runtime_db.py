"""Live nested execution scopes and concurrent recovery; no external providers."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from formal_plan_support import advance
from formal_recursive_support import setup_recursive
from sqlalchemy import text
from test_formal_execution_db import approve
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_execution import FormalExecution
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.formal_recursive import node_key
from app.infrastructure.workbench.repository import (
    SqlWorkbenchStores,
    WorkbenchRepository,
)


@pytest.mark.parametrize(
    "mutation",
    [
        "scope_expired",
        "future_parent_revision",
        "missing_scope",
        "resource_grant",
        "root_revoked",
    ],
)
async def test_nested_live_scope_cannot_be_bypassed_or_recreated(db, mutation):
    ctx = await setup_recursive(db)
    first = await advance(db, ctx)
    await approve(db, ctx.request, ctx.factory, first)
    pending = await advance(db, ctx)
    tid = pending["cases"][0]["task_id"]
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        parent = await repo.call_by_key(
            ctx.owner, str(task["session_id"]), node_key(tid, ("first",))
        )
        root = await repo.call_by_key(ctx.owner, str(task["session_id"]), "task:" + tid)
        assert parent is not None and root is not None
        async with repo.transaction():
            root_auth = await repo.method_authorization(
                ctx.owner, root["id"], lock=True
            )
            auth = await repo.method_authorization(ctx.owner, parent["id"], lock=True)
            assert auth is not None and root_auth is not None
            old = auth["revision"]
            if mutation == "missing_scope":
                await session.execute(
                    text(
                        "DELETE FROM workbench_method_authorizations WHERE call_id=:id"
                    ),
                    {"id": parent["id"]},
                )
            else:
                if mutation == "scope_expired":
                    auth["expires_at"] = (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat()
                elif mutation == "future_parent_revision":
                    auth["parent_revision"] = root_auth["revision"] + 10
                elif mutation == "resource_grant":
                    auth["grants"] = root_auth["grants"]
                elif mutation == "root_revoked":
                    auth, old = root_auth, root_auth["revision"]
                    auth["revoked"] = True
                auth["revision"] = old + 1
                await repo.save_method_authorization(
                    ctx.owner, str(task["session_id"]), auth, previous=old
                )
    with pytest.raises(CapabilityError):
        await approve(db, ctx.request, ctx.factory, pending)
    result = await advance(db, ctx)
    assert result["status"] == "failed", result
    assert len(ctx.remote.calls) == 1
    async with db() as session:
        repo = WorkbenchRepository(session)
        if mutation == "missing_scope":
            assert await repo.method_authorization(ctx.owner, parent["id"]) is None
        current = await repo.call_by_key(
            ctx.owner, str(task["session_id"]), node_key(tid, ("first",))
        )
        assert current is not None and current["status"] == "failed"


async def test_parallel_recursive_drivers_keep_nested_identity_and_exact_grants(db):
    ctx = await setup_recursive(db)
    pending = await advance(db, ctx)
    tid = pending["cases"][0]["task_id"]
    for index in range(4):
        await approve(db, ctx.request, ctx.factory, pending)
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                *(
                    FormalExecution(SqlWorkbenchStores(db), ctx.factory).drive(tid)
                    for _ in range(2)
                ),
                return_exceptions=True,
            ),
            timeout=30,
        )
        assert not any(isinstance(value, BaseException) for value in outcomes), outcomes
        pending = await advance(db, ctx)
        assert len(ctx.remote.calls) == index + 1
    assert pending["status"] == "passed", pending
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(tid)
        root = await repo.call_by_key(ctx.owner, str(task["session_id"]), "task:" + tid)
        assert root is not None
        root_auth = await repo.method_authorization(ctx.owner, root["id"])
        assert root_auth is not None and len(root_auth["grants"]) == 4
        attempts = await repo.list_attempts(tid)
        assert len(attempts) == 4
        assert len({tuple(a["refs"]["path"]) for a in attempts}) == 4
        for path in (("first",), ("second",)):
            call = await repo.call_by_key(
                ctx.owner, str(task["session_id"]), node_key(tid, path)
            )
            assert call is not None and call["status"] == "succeeded"
            scope = await repo.method_authorization(ctx.owner, call["id"])
            assert scope is not None and scope["grants"] == []
            assert scope["expires_at"] == root_auth["expires_at"]
        for attempt in attempts:
            refs = attempt["refs"]
            leaf_auth = await repo.method_authorization(
                ctx.owner, refs["method_call_id"]
            )
            assert leaf_auth is not None and leaf_auth["parent_call_id"] == root["id"]
            assert len(leaf_auth["grants"]) == 1
            grant = leaf_auth["grants"][0]
            assert (
                not grant["delegable"]
                and grant["recipient_call_id"] == refs["method_call_id"]
            )
            assert grant.get("resource_authority") is None


async def test_read_pins_the_producing_step_export_kind_and_version(db):
    from app.application.workbench.formal_recursive_runtime import (
        read_artifact,
        store_result,
    )
    from app.domain.workbench.capabilities import digest

    ctx = await setup_recursive(db)
    pending = await advance(db, ctx)
    task = {"id": pending["cases"][0]["task_id"]}
    result = {"outputs": {"total": 12}, "outputs_digest": digest({"total": 12})}
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            first = await store_result(repo, task, ("first",), result)
            second = await store_result(repo, task, ("second",), result)
            exported = await store_result(repo, task, ("first",), result, exports=True)
            assert (
                await read_artifact(repo, task, ("first",), first, result)
                == result["outputs"]
            )
            assert (
                await read_artifact(
                    repo, task, ("first",), exported, result, exports=True
                )
                == result["outputs"]
            )
            for ref in (
                second,
                exported,
                {**first, "version": None},
                {**first, "version": 0},
            ):
                with pytest.raises(CapabilityError, match="planned output version"):
                    await read_artifact(repo, task, ("first",), ref, result)
