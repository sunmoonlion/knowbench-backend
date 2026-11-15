"""Changing authority and ambiguous tool results cannot become fresh method success."""

import json
from copy import deepcopy

import pytest
from formal_session_model_support import (
    setup,
    shutdown,
    start_turn,
    wait_call,
    wait_completed,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_capabilities_db import OWNER, approval
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_method_calls import FormalMethodCalls
from app.domain.workbench.capabilities import SelectionPut
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("outcome", ["typed_failure", "unknown", "success"])
async def test_same_model_invocation_is_not_redispatched_and_method_output_is_typed(
    db, outcome
):
    _, factory, remote, sid, sandbox, fake, runner = await setup(db)
    fake.twice, fake.repeat_identity = True, True
    if outcome == "unknown":
        remote.fail = True
    elif outcome == "typed_failure":
        original = remote.call

        async def wrong_output(*args):
            result = await original(*args)
            return {**result, "structuredContent": {"receipt": 42}}

        remote.call = wrong_output
    try:
        await start_turn(db, runner, sid, sandbox)
        call = await wait_call(db, sid)
        assert call, fake.failures
        async with AsyncClient(
            transport=ASGITransport(fake.app), base_url="http://testserver"
        ) as http:
            result = await http.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json=approval(call).model_dump(),
            )
            assert result.status_code == 200, result.text
        await wait_completed(db, sid)
        expected = {
            "typed_failure": "failed",
            "unknown": "unknown",
            "success": "succeeded",
        }[outcome]
        assert len(remote.calls) == 1 and len(fake.responses) == 2
        for response in fake.responses:
            wrapper = response["result"]
            actual = json.loads(wrapper["content"][0]["text"])
            assert actual["status"] == expected
            assert wrapper["isError"] == (outcome != "success")
            if outcome != "success":
                assert actual["result"]["outputs"] is None
        async with db() as session:
            repo = WorkbenchRepository(session)
            history = await FormalMethodCalls(factory(repo)).list_calls(OWNER, sid)
            assert len(history) == 1 and history[0]["status"] == expected
    finally:
        await shutdown(runner, fake)


@pytest.mark.parametrize(
    "change", ["deselected", "source_disabled", "thread_changed", "reconnected"]
)
async def test_model_approval_rechecks_live_context_and_authority(db, change):
    frozen, factory, remote, sid, sandbox, fake, runner = await setup(db)
    try:
        await start_turn(db, runner, sid, sandbox)
        call = await wait_call(db, sid)
        assert call, fake.failures
        async with db() as session:
            repo = WorkbenchRepository(session)
            if change == "deselected":
                caps = factory(repo)
                current = await caps.selection(OWNER, sid)
                await caps.save_selection(
                    OWNER, sid, SelectionPut(revision=current["revision"])
                )
            else:
                async with repo.transaction():
                    if change == "source_disabled":
                        await session.execute(
                            text(
                                "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                            ),
                            {"id": str(frozen.draft.source.asset_id)},
                        )
                    elif change == "thread_changed":
                        await session.execute(
                            text(
                                "UPDATE workbench_sessions SET thread_id='replaced-thread' WHERE id=:id"
                            ),
                            {"id": sid},
                        )
                    else:
                        await repo.expire_pending_tool_approvals(
                            sandbox_id=sandbox, reason="runner replaced"
                        )
                        await repo.capability_revoke_sandbox(sandbox)
        async with AsyncClient(
            transport=ASGITransport(fake.app), base_url="http://testserver"
        ) as http:
            response = await http.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json=approval(call).model_dump(),
            )
            assert response.status_code in (200, 400, 403, 409), response.text
            if response.status_code == 200:
                assert response.json()["status"] == "expired"
        assert not remote.calls
        async with db() as session:
            repo = WorkbenchRepository(session)
            assert (
                await repo.method_authorization(OWNER, call["parent_call_id"]) is None
            )
    finally:
        await shutdown(runner, fake)


async def test_gateway_rejects_unobserved_identity_and_changed_arguments(db):
    _, _, remote, sid, sandbox, fake, runner = await setup(db)
    try:
        await start_turn(db, runner, sid, sandbox)
        call = await wait_call(db, sid)
        assert call, fake.failures
        invocation = call["detail"]["invocation"]
        connection = fake.thread_configs[invocation["thread_id"]][
            "mcp_servers.sunmoon_capabilities"
        ]
        request = {
            "id": 88,
            "method": "tools/call",
            "params": {
                "name": fake.tool_name,
                "arguments": call["detail"]["arguments"],
                "_meta": {
                    "threadId": invocation["thread_id"],
                    "callId": "unobserved-call",
                    "x-codex-turn-metadata": {
                        "thread_id": invocation["thread_id"],
                        "turn_id": invocation["turn_id"],
                    },
                },
            },
        }
        async with AsyncClient(
            transport=ASGITransport(fake.app), base_url="http://testserver"
        ) as http:
            response = await http.post(
                connection["url"], headers=connection["http_headers"], json=request
            )
            assert response.status_code == 200, response.text
            assert response.json()["error"] == {
                "code": -32001,
                "message": "tool call has not been verified by the runner",
            }
            changed = deepcopy(request)
            changed["params"]["_meta"]["callId"] = invocation["call_id"]
            changed["params"]["arguments"] = {"to": "other@example.test"}
            response = await http.post(
                connection["url"], headers=connection["http_headers"], json=changed
            )
            assert response.status_code == 200, response.text
            assert response.json()["error"] == {
                "code": -32001,
                "message": "tool call differs from the runner's observed call",
            }
        assert not remote.calls
        async with db() as session:
            assert (
                len(await WorkbenchRepository(session).capability_calls(OWNER, sid))
                == 1
            )
    finally:
        await shutdown(runner, fake)
