"""Recovery must not invert an existing method's Task/Session/scope locks."""

import asyncio

import pytest
from test_capabilities_db import OWNER, service
from test_method_capabilities_db import setup_method
from test_workbench_ledger_db import db as db  # noqa: F401

from app.domain.workbench.capabilities import MethodPermissionRevoke
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("operation", ["revoke", "prepare"])
async def test_method_scope_waits_on_task_before_bulk_recovery_needs_selection(
    db, operation
):
    sid, task_id, _, _, _, _, _, _, remote = await setup_method(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        current = await service(repo, remote).selection(OWNER, sid)
        sandbox = str((await repo.get_session(sid))["sandbox_id"])
    waiting = asyncio.Event()

    class ObservedRepository(WorkbenchRepository):
        async def get_task(self, target, *, owner_actor_id=None, for_update=False):
            if for_update and str(target) == task_id:
                waiting.set()
            return await super().get_task(
                target, owner_actor_id=owner_actor_id, for_update=for_update
            )

    async def mutate_scope():
        async with db() as session:
            svc = service(ObservedRepository(session), remote)
            if operation == "revoke":
                return await svc.revoke_method_permissions(
                    OWNER,
                    sid,
                    MethodPermissionRevoke(
                        revision=current["revision"],
                        method_context=current["method"]["context"],
                    ),
                )
            return await svc.prepare(OWNER, sid, scene="method")

    worker = None
    try:
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                await repo.get_task(task_id, for_update=True)
                worker = asyncio.create_task(mutate_scope())
                await asyncio.wait_for(waiting.wait(), 5)
                # Before the fix, the waiting worker already owned Selection;
                # recovery then waited on it while owning the Task it needed.
                await asyncio.wait_for(repo.capability_revoke_sandbox(sandbox), 5)
                await asyncio.wait_for(
                    repo.expire_pending_tool_approvals(
                        sandbox_id=sandbox, reason="test recovery"
                    ),
                    5,
                )
        result = await asyncio.wait_for(worker, 5)
        if operation == "revoke":
            assert result["method"]["permission_status"] == "revoked"
        else:
            assert (
                result["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is True
            )
        assert remote.calls == []
    finally:
        if worker and not worker.done():
            worker.cancel()
        if worker:
            await asyncio.gather(worker, return_exceptions=True)
