"""Probe metadata cannot expand authority; unresolved costs retain their ledger."""

import pytest
from formal_model_support import ModelServer
from httpx import ASGITransport, AsyncClient
from test_capabilities_db import OWNER
from test_formal_model_execution_db import drive_to_approval
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_task_runtime_db import close

from app.application.workbench.interactions import cancel_task
from app.domain.workbench.formal_model import PROBE_SERVER, PROBE_TOOL
from app.infrastructure.workbench.repository import WorkbenchRepository


class BoundaryServer(ModelServer):
    async def _handle(self, ws, msg):
        if msg["method"] == "turn/start":
            connection = self.thread_configs[msg["params"]["threadId"]][
                "mcp_servers." + PROBE_SERVER
            ]
            async with AsyncClient(
                transport=ASGITransport(self.app), base_url="http://testserver"
            ) as http:

                async def rpc(method, params=None):
                    return await http.post(
                        connection["url"],
                        headers=connection["http_headers"],
                        json={"id": 90, "method": method, "params": params or {}},
                    )

                listing = await rpc("tools/list")
                assert [t["name"] for t in listing.json()["result"]["tools"]] == [
                    PROBE_TOOL
                ]
                for method in (
                    "resources/list",
                    "resources/templates/list",
                    "resources/read",
                ):
                    response = await rpc(method, {"uri": "file:///etc/passwd"})
                    assert response.json()["error"]["code"] == -32601
                    assert "result" not in response.json()
                response = await rpc(
                    "tools/call", {"name": "unrelated_tool", "arguments": {}}
                )
                assert response.status_code == 403
                assert (
                    "Only the pinned candidate" in response.json()["error"]["message"]
                )
                self.boundaries_checked = True
        await super()._handle(ws, msg)


async def test_probe_http_has_no_resources_or_foreign_tools(db):
    fake, runner = BoundaryServer(), None
    await fake.start()
    try:
        _, remote, _, _, runner, _, call = await drive_to_approval(db, fake)
        assert call is not None, fake.failures
        assert fake.boundaries_checked and len(remote.calls) == 1
    finally:
        if runner:
            await close(runner)
        await fake.close()


async def test_cancelling_owner_approval_keeps_live_turn_until_actual_completion(db):
    fake, runner = ModelServer(), None
    await fake.start()

    async def cancel_waiting(task_id):
        async with db() as session:
            repo = WorkbenchRepository(session)
            assert (await repo.get_task(task_id))["state"] == "WAITING"
            await cancel_task(repo, task_id, owner_actor_id=OWNER)
            task = await repo.get_task(task_id)
            assert task["original_input"]["client_context"]["turn_open"]
            assert (await repo.list_attempts(task_id))[0]["refs"]["usage_pending"]

    try:
        _, remote, _, _, runner, task_id, call = await drive_to_approval(
            db, fake, decision=None, before_wait=cancel_waiting
        )
        assert call is not None, fake.failures
        assert remote.calls == []
        async with db() as session:
            task = await WorkbenchRepository(session).get_task(task_id)
            assert task["state"] == "CANCELLED"
            assert not task["original_input"]["client_context"].get("turn_open")
    finally:
        if runner:
            await close(runner)
        await fake.close()


@pytest.mark.parametrize("usage", [False, True])
async def test_probe_cleanup_retains_unsettled_costs_but_allows_settled_evidence(
    db, usage
):
    fake, runner = ModelServer(usage=usage), None
    await fake.start()
    try:
        request, _, _, conductor, runner, task_id, call = await drive_to_approval(
            db, fake
        )
        assert call is not None, fake.failures
        for _ in range(3):
            await conductor.advance(str(request.run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            if usage:
                async with repo.transaction():
                    await repo.purge_test_run(str(request.run_id))
                cases = await repo.list_test_cases(str(request.run_id))
                assert cases[0]["task_id"] is None
            else:
                with pytest.raises(ValueError, match="usage and stopped execution"):
                    async with repo.transaction():
                        await repo.purge_test_run(str(request.run_id))
                attempts = await repo.list_attempts(task_id)
                assert attempts[0]["refs"]["usage_pending"]
                assert (await repo.list_test_cases(str(request.run_id)))[0]["task_id"]
    finally:
        if runner:
            await close(runner)
        await fake.close()
