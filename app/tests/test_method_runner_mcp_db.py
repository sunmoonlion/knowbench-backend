"""Runner websocket -> real MCP HTTP -> data grant -> accepted method result.

The app-server and knowbase ports are synthetic; no model or financial data.
The envelope shape is checked separately against the pinned binary probe.
"""

import json

import pytest
from method_plan_fixtures import catalog, proposal
from sqlalchemy import text
from test_method_advisor_db import ROWS
from test_workbench_advisor_db import ScriptedAppServer, close, drive, seed
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_mcp_routes import ISSUER, URL, FakeDataMcp, client
from test_workbench_mcp_routes import served as served

from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.asyncio


async def test_runner_observation_authorizes_only_the_current_method_data_grant(
    served, db, monkeypatch
):
    from app.interfaces.mcp import workbench_mcp

    kb = FakeDataMcp()
    monkeypatch.setattr(workbench_mcp, "knowbase_v2", lambda: kb)
    responses = []
    headers = {}
    task_id = ""

    class ToolServer(ScriptedAppServer):
        async def handle(self, ws, msg):
            if msg["method"] != "turn/start" or len(self.turn_inputs) != 1:
                await super().handle(ws, msg)
                return

            class WithTool:
                async def send(self, raw):
                    event = json.loads(raw)
                    if event.get("method") == "item/completed":
                        params = event["params"]
                        thread, turn = params["threadId"], params["turnId"]
                        args = {"task": task_id, "material_id": "synthetic-document"}
                        item = {
                            "type": "mcpToolCall",
                            "id": "call-data",
                            "server": "sunmoon_workbench",
                            "tool": "read_input",
                            "arguments": args,
                            "status": "inProgress",
                        }
                        await ws.send(
                            json.dumps(
                                {
                                    "method": "item/started",
                                    "params": {
                                        "threadId": thread,
                                        "turnId": turn,
                                        "item": item,
                                    },
                                }
                            )
                        )
                        message = {
                            "jsonrpc": "2.0",
                            "id": 8,
                            "method": "tools/call",
                            "params": {
                                "name": "read_input",
                                "arguments": args,
                                "_meta": {
                                    "callId": "call-data",
                                    "threadId": thread,
                                    "x-codex-turn-metadata": {
                                        "thread_id": thread,
                                        "turn_id": turn,
                                    },
                                },
                            },
                        }
                        async with client(served) as http:
                            response = await http.post(
                                URL, headers=headers, json=message
                            )
                        responses.append(response.json())
                        await ws.send(
                            json.dumps(
                                {
                                    "method": "item/completed",
                                    "params": {
                                        "threadId": thread,
                                        "turnId": turn,
                                        "item": {
                                            **item,
                                            "status": "completed",
                                            "result": response.json().get("result"),
                                        },
                                    },
                                }
                            )
                        )
                    await ws.send(raw)

            await super().handle(WithTool(), msg)

    fake = ToolServer([proposal().model_dump_json(), ROWS, ROWS])
    await fake.start()
    runner = None
    try:
        runner, _, sandbox, task_id = await seed(db, fake)
        runner.records = True
        headers = {
            "Authorization": "Bearer "
            + ISSUER.records_token("u-abc", owner_actor_id=OWNER, sandbox=sandbox).token
        }
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                await session.execute(
                    text(
                        "UPDATE workbench_tasks SET pack_source='knowmart', pack_snapshot=cast(:p as jsonb), pack_digest=:d, profile_version='1' WHERE id=:id"
                    ),
                    {
                        "p": catalog().model_dump_json(),
                        "d": "sha256:" + "a" * 64,
                        "id": task_id,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO auth_user(id, issuer, subject, roles, scopes) VALUES (:id, 'test', 'author', '[]', '[]')"
                    ),
                    {"id": OWNER},
                )
                await repo.insert_task_resolution(
                    {
                        "task_id": task_id,
                        "resolution_id": "synthetic-resolution",
                        "revision": 1,
                        "consumer": "expert:synthetic@1",
                        "params": {},
                        "grant_ref": "only-this-task",
                        "status": "ready",
                    }
                )
        await drive(runner)
        assert len(responses) == 1
        assert responses[0]["result"]["isError"] is False, responses
        assert [m["method"] for _, m in kb.seen] == ["tools/list", "tools/call"]
        claims, request = kb.seen[-1]
        assert claims["tools"] == ["read_input"]
        assert request["params"]["arguments"] == {
            "material_id": "synthetic-document",
            "run": "only-this-task",
        }
        async with db() as session:
            repo = WorkbenchRepository(session)
            row = await repo.get_task(task_id)
            assert row["state"] == TaskState.SUCCEEDED
            attempts = await repo.list_attempts(task_id)
            assert all(a["refs"]["runtime_turn_id"] in a["turn_ids"] for a in attempts)
            assert (await repo.get_artifact(task_id=task_id, name="handback"))[
                "content"
            ]["final_outputs"]
    finally:
        if runner:
            await close(runner)
        await fake.close()
