"""Real nested handoff, owner-only adjudication and late provider receipts."""

import asyncio

import pytest
from formal_plan_support import advance
from formal_recursive_support import setup_recursive
from test_formal_execution_db import approve
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db

from app.application.workbench.delegation_receipts import HandoffClosure, handoffs
from app.application.workbench.ledger import Ledger
from app.domain.workbench.errors import IdempotencyConflict, NotFound, StaleStateVersion
from app.infrastructure.workbench.repository import WorkbenchRepository


def decision(parent):
    return HandoffClosure.model_validate(
        {
            "parent_call_id": parent["parent_call_id"],
            "scope_digest": parent["scope_digest"],
            "residual": "Owner will open a new task for omitted roles"
            if parent["missing"]
            else None,
            "decisions": [
                {
                    "child_call_id": c["child_call_id"],
                    "evidence_digest": c["evidence_digest"],
                    "decision": "accept" if c["state"] == "COMPLETED" else "defer",
                    "reason": "Verified the pinned output or retained the interruption",
                    "next_owner": "requester" if c["state"] != "COMPLETED" else None,
                }
                for c in parent["children"]
            ],
        }
    )


@pytest.mark.parametrize("cancel", [True, False])
async def test_nested_parent_closure_and_late_receipt(db, cancel):
    ctx = await setup_recursive(db)
    pending = await advance(db, ctx)
    tid = pending["cases"][0]["task_id"]

    async def view(command=None, owner=ctx.owner):
        async with db() as s:
            return (await handoffs(WorkbenchRepository(s), owner, tid, command))[
                "parents"
            ]

    with pytest.raises(NotFound):
        await view(owner=OTHER)
    issued = await view()
    assert issued and all(not p["can_close"] for p in issued)
    started = asyncio.Event()
    release = asyncio.Event()
    if cancel:
        original = ctx.remote.call

        async def delayed(*args):
            started.set()
            await release.wait()
            return await original(*args)

        ctx.remote.call = delayed
        action = asyncio.create_task(approve(db, ctx.request, ctx.factory, pending))
        try:
            await asyncio.wait_for(started.wait(), 10)
            async with db() as session:
                await Ledger(WorkbenchRepository(session)).request_cancel(
                    tid, owner_actor_id=ctx.owner
                )
            before = await view()
            group = next(p for p in before if p["can_close"])
            assert group["missing"]
            cmd = decision(group)
            with pytest.raises(StaleStateVersion):
                await view(cmd.model_copy(update={"residual": None}))
            await view(cmd)
        finally:
            release.set()
            await action
        records = await view()
        r = next(
            r for p in records for c in p["children"] for r in c["rounds"] if r["late"]
        )
        assert r["late"][0]["status"] == "succeeded"
        assert r["receipts"][-1]["state"] == "CANCELLED"
        assert len(ctx.remote.calls) == 1
    else:
        for _ in range(4):
            await approve(db, ctx.request, ctx.factory, pending)
            pending = await advance(db, ctx)
        assert pending["status"] == "passed"
    groups = await view()
    assert len(groups) == (2 if cancel else 3)
    root = next(
        p for p in groups if len(p["children"][0]["rounds"][0]["request"]["role"]) == 1
    )
    if not cancel:
        with pytest.raises(StaleStateVersion):
            await view(decision(root))
    for group in sorted(groups, key=lambda p: p is root):
        cmd = decision(group)
        first, second = await asyncio.gather(view(cmd), view(cmd))

        def result(rows, parent_id=group["parent_call_id"]):
            return next(p["closure"] for p in rows if p["parent_call_id"] == parent_id)

        assert result(first) == result(second)
        assert result(first)["writer"] == ctx.owner
        with pytest.raises(IdempotencyConflict):
            await view(cmd.model_copy(update={"scope_digest": "0" * 64}))
    final = await view()
    assert all(p["closure"] for p in final)
    if not cancel:
        from app.application.workbench.delegation_receipts import handoff_evidence

        children = [c for p in final for c in p["children"]]
        child = children[0]
        ref = child["rounds"][-1]["receipts"][-1]["artifacts"][0]
        async with db() as session:
            repo = WorkbenchRepository(session)
            assert (
                await handoff_evidence(
                    repo, ctx.owner, tid, child["child_call_id"], ref["id"]
                )
            )["outputs"]
            with pytest.raises(NotFound):
                await handoff_evidence(
                    repo, OTHER, tid, child["child_call_id"], ref["id"]
                )
            with pytest.raises(NotFound):
                await handoff_evidence(
                    repo, ctx.owner, tid, children[1]["child_call_id"], ref["id"]
                )
    assert not any(
        "outputs" in receipt
        for p in final
        for c in p["children"]
        for r in c["rounds"]
        for receipt in r["receipts"]
    )
