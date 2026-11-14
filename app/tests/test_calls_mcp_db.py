"""Real authenticated MCP entry records/replays a call without rerunning dispatch."""

import pytest
from httpx import AsyncClient
from sample_replies import PROFILE, SCOPE
from sqlalchemy import text
from test_workbench_advisor_db import close
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_mcp_routes import URL, bearer, client, tool
from test_workbench_mcp_routes import served as served
from test_workbench_records_db import world
from test_workbench_review_db import unbalanced
from tool_invocation_support import ObservedPlatformTransport

from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")


async def test_platform_mcp_writes_the_shared_ledger_and_replays_only_the_receipt(
    served, db, monkeypatch
):
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        message = tool("list_project_conversations", task=ids["task"])
        async with AsyncClient(
            transport=ObservedPlatformTransport(served, db, ids),
            base_url="http://testserver",
        ) as http:
            first = (await http.post(URL, json=message, headers=bearer())).json()
        call_id = first["result"]["_meta"]["call_id"]
        async with db() as s:
            repo = WorkbenchRepository(s)
            row = (
                (
                    await s.execute(
                        text("SELECT * FROM workbench_calls WHERE id=:id"),
                        {"id": call_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["origin"] == "platform" and row["status"] == "succeeded"
            assert row["source_asset_id"] is None and row["connection_id"] is None
            assert (
                str(row["task_id"]) == ids["task"] and row["parent_call_id"] is not None
            )
            identity = row["detail"]["invocation"]
            assert not await repo.capability_calls(OWNER, str(row["session_id"]))
        message["params"]["_meta"] = {
            "threadId": identity["thread_id"],
            "callId": identity["call_id"],
            "x-codex-turn-metadata": {
                "thread_id": identity["thread_id"],
                "turn_id": identity["turn_id"],
            },
        }
        message["id"] = 19

        async def never_dispatch(*args, **kwargs):
            raise AssertionError("duplicate invocation must not dispatch again")

        monkeypatch.setattr("app.interfaces.mcp.workbench_mcp.dispatch", never_dispatch)
        async with client(served) as http:
            again = (await http.post(URL, json=message, headers=bearer())).json()
        assert again["id"] == 19 and again["result"] == first["result"]
        async with db() as s:
            assert (
                await s.scalar(
                    text("SELECT count(*) FROM workbench_calls WHERE origin='platform'")
                )
                == 1
            )
    finally:
        await close(runner)
        await fake.close()
