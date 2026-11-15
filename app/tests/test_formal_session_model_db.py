"""Ordinary model calls share exact approval and typed results without expert tasks."""

import json

import pytest
from formal_session_model_support import (
    setup,
    shutdown,
    start_turn,
    wait_call,
    wait_completed,
)
from httpx import ASGITransport, AsyncClient
from test_capabilities_db import OWNER, approval
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_method_calls import FormalMethodCalls
from app.domain.workbench.formal_method_calls import FormalMethodInvocations
from app.domain.workbench.method_authorization import MethodAuthorization
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("kind", ["chat", "work"])
@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_model_method_uses_user_turn_exact_approval_and_typed_result(
    db, kind, decision
):
    frozen, factory, remote, sid, sandbox, fake, runner = await setup(db, kind=kind)
    try:
        await start_turn(db, runner, sid, sandbox)
        call = await wait_call(db, sid)
        assert call is not None, fake.failures
        assert call["detail"]["execution_kind"] == "formal_method_model"
        assert call["task_id"] is None and call["attempt_id"] is None
        assert not remote.calls
        async with AsyncClient(
            transport=ASGITransport(fake.app), base_url="http://testserver"
        ) as http:
            response = await http.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json=approval(call, decision=decision).model_dump(),
            )
            assert response.status_code == 200, response.text
            history = await http.get(
                f"/api/workbench/sessions/{sid}/formal-method-calls"
            )
            assert history.status_code == 200, history.text
            parsed = FormalMethodInvocations.model_validate(history.json())
            assert parsed.calls[0].reference == frozen.reference
            assert parsed.calls[0].call.detail.execution_kind == "formal_method_model"
        events = await wait_completed(db, sid)
        assert (
            len(fake.turn_inputs) == 1
            and "expected-never-sent" not in fake.turn_inputs[0]
        )
        assert not fake.failures
        assert len(remote.calls) == (1 if decision == "accept" else 0)
        actual = json.loads(fake.responses[0]["result"]["content"][0]["text"])
        assert actual["status"] == ("succeeded" if decision == "accept" else "declined")
        assert actual["result"]["outputs"] == (
            {"receipt": "expected-never-sent"} if decision == "accept" else None
        )
        async with db() as session:
            repo = WorkbenchRepository(session)
            talk = await repo.get_session(sid)
            assert talk["wheel"] == "user" and talk["active_task_id"] is None
            history = await FormalMethodCalls(factory(repo)).list_calls(OWNER, sid)
            root = await repo.call_by_key(
                OWNER, sid, "formal-session:" + history[0]["request_id"]
            )
            assert root["parent_call_id"] is None and root["task_id"] is None
            auth = await repo.method_authorization(OWNER, root["id"])
            if decision == "accept":
                grant = MethodAuthorization.model_validate(auth).grants[0]
                assert (
                    not grant.delegable and str(grant.recipient_call_id) == root["id"]
                )
            else:
                assert auth is None
        assert any(e["type"] == "thread/tokenUsage/updated" for e in events)
        assert not any(e["type"].startswith("task/") for e in events)
    finally:
        await shutdown(runner, fake)
