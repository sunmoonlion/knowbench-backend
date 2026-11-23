"""Real authenticated MCP entry records/replays a call without rerunning dispatch."""

import pytest
from httpx import AsyncClient
from sample_replies import PROFILE, SCOPE
from sqlalchemy import text
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_mcp_routes import URL, bearer, client, tool
from test_workbench_mcp_routes import served as served
from test_workbench_records_db import talk
from test_workbench_review_db import unbalanced
from test_workbench_task_runtime_db import close
from tool_invocation_support import ObservedPlatformTransport
from tool_invocation_support import running_platform_world as world

from app.domain.workbench.native_functions import (
    COMPUTE,
    LOCAL_REVISIONS,
    PROJECT_LIST,
    PROJECT_READ,
)
from app.infrastructure.workbench.repository import WorkbenchRepository

pytestmark = pytest.mark.usefixtures("builtin_packs_on_offer")


@pytest.mark.parametrize("operation", [PROJECT_LIST, PROJECT_READ])
async def test_platform_mcp_writes_the_shared_ledger_and_replays_only_the_receipt(
    served, db, monkeypatch, operation
):
    fake, runner, ids = await world(db, [SCOPE, PROFILE, unbalanced()])
    try:
        args = {"task": ids["task"]}
        if operation == PROJECT_READ:
            async with db() as s:
                conversation = await talk(
                    WorkbenchRepository(s),
                    ids["project"],
                    "chat",
                    [(f"question {n}", f"answer {n}") for n in range(21)],
                )
            args.update(conversation=conversation, page=2)
        message = tool(operation, **args)
        async with AsyncClient(
            transport=ObservedPlatformTransport(served, db, ids),
            base_url="http://testserver",
        ) as http:
            first = (await http.post(URL, json=message, headers=bearer())).json()
        if operation == PROJECT_READ:
            page = first["result"]["structuredContent"]
            assert (page["page"], page["pages"]) == (2, 2)
            assert [entry["said"] for entry in page["entries"]] == [
                "question 20",
                "answer 20",
            ]
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
            assert (
                await s.scalar(
                    text(
                        "SELECT count(*) FROM workbench_session_events WHERE event_type='records/read' AND payload->>'call_id'=:cid"
                    ),
                    {"cid": call_id},
                )
                == 1
            )
            revision = LOCAL_REVISIONS[operation]()
            assert row["detail"]["operation"][
                "registered_source"
            ] == revision.model_dump(exclude={"contract"})
            if operation == PROJECT_READ:
                [event] = [
                    e["payload"]
                    for e in await repo.list_events(session_id=ids["current"])
                    if e["type"] == "records/read"
                ]
                assert event == {
                    "tool": operation,
                    "conversation": args["conversation"],
                    "page": 2,
                    "call_id": call_id,
                }
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


@pytest.mark.parametrize("operation", list(LOCAL_REVISIONS))
async def test_removed_local_registration_blocks_listing_and_inflight_dispatch(
    served, db, monkeypatch, operation
):
    from test_workbench_mcp_routes import rpc

    from app.interfaces.mcp import workbench_mcp

    fake, runner, ids = await world(
        db,
        [SCOPE, PROFILE, unbalanced()],
        extra_tools=("discover_source_tools", "find_subject", "find_expert"),
    )
    original = workbench_mcp.dispatch
    args = {"task": ids["task"]}
    if operation == PROJECT_READ:
        args["conversation"] = ids["chat"]
    elif operation == COMPUTE:
        args.update(name="reuse", expression="x", inputs={"x": "c1"})

    elif operation == "request_data":
        args["needs"] = [{"reason": "test"}]
    elif operation == "find_subject":
        args["name"] = "test"

    async def withdraw(*args, **kwargs):
        async with db() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM workbench_registry_service_functions WHERE provider='knowbench' AND name=:name AND version=1"
                ),
                {"name": operation},
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(workbench_mcp, "dispatch", withdraw)
    try:
        async with AsyncClient(
            transport=ObservedPlatformTransport(served, db, ids),
            base_url="http://testserver",
        ) as http:
            response = (
                await http.post(
                    URL,
                    json=tool(operation, **args),
                    headers=bearer(),
                )
            ).json()["result"]
            assert response["isError"] and response["_meta"]["call_status"] == "failed"
            assert "changed before dispatch" in response["content"][0]["text"]
            tools = (
                await http.post(URL, json=rpc("tools/list"), headers=bearer())
            ).json()["result"]["tools"]
            assert all(t["name"] != operation for t in tools)
            again = (
                await http.post(
                    URL,
                    json=tool(operation, **args),
                    headers=bearer(),
                )
            ).json()
            assert "not registered" in again["result"]["content"][0]["text"]
        async with db() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM workbench_calls WHERE origin='platform'")
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM workbench_session_events WHERE event_type IN ('records/read','knowbot/compute')"
                    )
                )
                == 0
            )
    finally:
        await close(runner)
        await fake.close()


async def test_registered_data_planning_and_expert_directory(served, db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from test_find_expert_db import FREE, Directory
    from test_workbench_expert_data_db import PROPOSED

    from app.interfaces.mcp import workbench_mcp

    supplier = {
        "channel": "owner-channel",
        "service": "documents",
        "services": ["documents"],
        "license": "self",
        "kind": "supplier_response",
        "readings": ["original", "text"],
        "name": "Owner supplier",
        "tools": [{"name": "report", "inputSchema": {"type": "object"}}],
    }
    subjects = [
        {"subject": code, "name": "同名"} for code in ("subject-a", "subject-b")
    ]
    feed = SimpleNamespace(
        configured=lambda: True,
        securities=AsyncMock(return_value=subjects),
        source_tools=AsyncMock(return_value={"sources": [supplier]}),
    )
    directory = Directory([FREE])
    base = SimpleNamespace(
        add_needs=AsyncMock(
            return_value={
                "revision": 2,
                "items": [{"need_key": "req1_1", "status": "to_confirm"}],
            }
        )
    )
    monkeypatch.setattr(workbench_mcp, "knowfeed_supply", lambda: feed)
    monkeypatch.setattr(workbench_mcp, "knowmart_catalog", lambda: directory)
    monkeypatch.setattr(workbench_mcp, "knowbase_v2", lambda: base)
    calls = {
        "discover_source_tools": {"service": "documents"},
        "find_subject": {"name": "同名"},
        "find_expert": {},
        "request_data": {"needs": PROPOSED[:1]},
    }
    fake, runner, ids = await world(
        db,
        [SCOPE, PROFILE, unbalanced()],
        extra_tools=tuple(k for k in calls if k != "request_data"),
    )
    try:
        async with db() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO auth_user (id,issuer,subject,roles,scopes) VALUES (:owner,'https://casdoor.test','directory-owner','[]','[]')"
                ),
                {"owner": OWNER},
            )
            await WorkbenchRepository(session).insert_task_resolution(
                {
                    "task_id": ids["task"],
                    "resolution_id": "res-1",
                    "grant_ref": "knowbench:run:planning",
                    "revision": 1,
                    "consumer": "expert:k@1",
                    "params": {},
                    "status": "ready",
                }
            )
        answers = {}
        async with AsyncClient(
            transport=ObservedPlatformTransport(served, db, ids),
            base_url="http://testserver",
        ) as http:
            for name, fields in calls.items():
                message = tool(name, task=ids["task"], **fields)
                forged = tool(name, task=ids["task"], owner="another-user", **fields)
                denied = (await http.post(URL, json=forged, headers=bearer())).json()[
                    "result"
                ]
                assert denied["isError"]
                answer = (await http.post(URL, json=message, headers=bearer())).json()[
                    "result"
                ]
                assert answer["isError"] is False, answer
                answers[name] = answer
        assert answers["discover_source_tools"]["structuredContent"] == {
            "sources": [supplier]
        }
        assert answers["find_subject"]["structuredContent"] == {"found": subjects}
        [expert] = answers["find_expert"]["structuredContent"]["experts"]
        assert expert == {k: v for k, v in FREE.items() if k != "owned"}
        proposal = answers["request_data"]["structuredContent"]
        assert (proposal["status"], proposal["request"]) == ("listed", "r1")

        async with db() as session:
            repo = WorkbenchRepository(session)
            [proposal_row] = (await repo.get_task_resolution(ids["task"]))["detail"][
                "requests"
            ]
            assert (
                proposal_row["status"] == "pending"
                and proposal_row["call_id"]
                == answers["request_data"]["_meta"]["call_id"]
            )
            for name, answer in answers.items():
                row = (
                    (
                        await session.execute(
                            text("SELECT * FROM workbench_calls WHERE id=:id"),
                            {"id": answer["_meta"]["call_id"]},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert (
                    row["status"] == "succeeded" and str(row["task_id"]) == ids["task"]
                )
                op = row["detail"]["operation"]
                revision = LOCAL_REVISIONS[name]()
                assert op["registered_source"] == revision.model_dump(
                    exclude={"contract"}
                )
                assert (
                    op["required_permissions"] == revision.contract.required_permissions
                )
                assert op["effect"] == ("write" if name == "request_data" else "read")
                invocation = row["detail"]["invocation"]
                message = tool(name, task=ids["task"], **calls[name])
                message["params"]["_meta"] = {
                    "threadId": invocation["thread_id"],
                    "callId": invocation["call_id"],
                    "x-codex-turn-metadata": {
                        "thread_id": invocation["thread_id"],
                        "turn_id": invocation["turn_id"],
                    },
                }
                async with client(served) as http:
                    replay = (
                        await http.post(URL, json=message, headers=bearer())
                    ).json()["result"]
                assert replay == answer
        feed.source_tools.assert_awaited_once_with("directory-owner", "documents")
        feed.securities.assert_awaited_once_with("同名", 10)
        assert directory.asked == ["directory-owner"]
        base.add_needs.assert_awaited_once()
        assert base.add_needs.await_args.args[:2] == ("directory-owner", "res-1")
    finally:
        await close(runner)
        await fake.close()
