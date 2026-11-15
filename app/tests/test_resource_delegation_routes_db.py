"""Real HTTP/guard authorization with synthetic runner events and MCP supplier."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_capabilities_db import OWNER, approval
from test_formal_method_use_routes_db import application
from test_method_tools_db import check, observed
from test_resource_delegation_runtime_db import confirm, prepare, setup
from test_workbench_ledger_db import db as db  # noqa: F401
from tool_invocation_support import request_call

from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.method_authorization import MethodAuthorization
from app.infrastructure.workbench.repository import WorkbenchRepository


def selection_body(ctx, view, *, delegated=True):
    body = {
        "revision": view["revision"],
        "tools": [ctx.key],
        "skills": [],
        "method_context": view["method"]["context"],
        "purpose": "Deliver the notice",
    }
    if delegated:
        body["delegations"] = {ctx.key: ctx.record.reference.model_dump(mode="json")}
    return body


def change_catalog(ctx, change):
    if change == "empty":
        ctx.records.clear()
    elif change == "revoked":
        ctx.records[:] = [ctx.record.model_copy(update={"status": "revoked"})]
    elif change == "changed":
        # Same ID and revision: the full reviewed record must still match.
        ctx.records[:] = [ctx.record.model_copy(update={"issuer": "Changed issuer"})]
    else:
        assert change == "active"


@pytest.mark.parametrize("delegated", [False, True])
async def test_http_selection_preserves_explicit_reference_without_default_delegation(
    db, delegated
):
    ctx = await setup(db)
    path = f"/api/workbench/sessions/{ctx.sid}/capabilities"
    async with AsyncClient(
        transport=ASGITransport(application(db, ctx.service)),
        base_url="http://testserver",
    ) as http:
        response = await http.get(path)
        assert response.status_code == 200, response.text
        view = response.json()
        reference = ctx.record.reference.model_dump(mode="json")
        assert view["delegations"] == {}
        assert view["method"]["delegation_options"][0]["authority"] == reference
        saved = await http.put(
            path, json=selection_body(ctx, view, delegated=delegated)
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["delegations"] == (
            {ctx.key: reference} if delegated else {}
        )
        restored = await http.get(path)
        assert restored.status_code == 200, restored.text
        assert restored.json()["delegations"] == saved.json()["delegations"]
    opened, _ = await prepare(db, ctx)
    async with db() as session:
        repo = WorkbenchRepository(session)
        root = await repo.call_by_key(OWNER, ctx.sid, "task:" + ctx.tid)
        assert root is not None
        parent = MethodAuthorization.model_validate(
            await repo.method_authorization(OWNER, root["id"])
        )
        attempt = await repo.get_attempt(opened["attempt_id"])
        child = MethodAuthorization.model_validate(
            await repo.method_authorization(OWNER, attempt["refs"]["method_call_id"])
        )
        assert parent.grants[0].delegable is delegated
        assert child.grants[0].delegable is False
        assert child.grants[0].recipient_call_id == child.call_id
        assert child.grants[0].resource_authority == (
            ctx.record.reference if delegated else None
        )
    assert ctx.remote.calls == []


@pytest.mark.parametrize("change", ["empty", "revoked", "changed"])
async def test_http_selection_rejects_stale_resource_reference(db, change):
    ctx = await setup(db)
    path = f"/api/workbench/sessions/{ctx.sid}/capabilities"
    async with AsyncClient(
        transport=ASGITransport(application(db, ctx.service)),
        base_url="http://testserver",
    ) as http:
        response = await http.get(path)
        assert response.status_code == 200, response.text
        initial = response.json()
        change_catalog(ctx, change)
        response = await http.put(path, json=selection_body(ctx, initial))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "capability_unavailable"
        current = await http.get(path)
        assert current.status_code == 200, current.text
        view = current.json()
        assert view["revision"] == initial["revision"]
        assert view["delegations"] == {}
        options = view["method"]["delegation_options"]
        if change == "changed":
            assert options[0]["authority"] == ctx.records[0].reference.model_dump(
                mode="json"
            )
            assert options[0]["authority"] != ctx.record.reference.model_dump(
                mode="json"
            )
        else:
            assert options == []
    assert ctx.remote.calls == []


@pytest.mark.parametrize("change", ["active", "empty", "revoked", "changed"])
async def test_http_action_approval_rechecks_current_resource_authority(db, change):
    ctx = await setup(db)
    await confirm(db, ctx)
    _, token = await prepare(db, ctx)
    async with db() as session:
        repo = WorkbenchRepository(session)
        staged = await request_call(
            ctx.service(repo), ctx.sid, token, ctx.key, {"to": "test@example.test"}
        )
        call = await repo.capability_call(OWNER, staged["call_id"])
        assert call["status"] == "awaiting_approval"
    assert ctx.remote.calls == []
    change_catalog(ctx, change)
    async with AsyncClient(
        transport=ASGITransport(application(db, ctx.service)),
        base_url="http://testserver",
    ) as http:
        response = await http.post(
            f"/api/workbench/capability-calls/{call['id']}/decision",
            json=approval(call).model_dump(mode="json"),
        )
        if change == "active":
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "succeeded"
            assert len(ctx.remote.calls) == 1
        else:
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "capability_unavailable"
            assert ctx.remote.calls == []


@pytest.mark.parametrize("change", ["empty", "revoked"])
async def test_platform_guard_rechecks_same_observed_call_against_current_catalog(
    db, change
):
    ctx = await setup(db)
    await confirm(db, ctx)
    opened, _ = await prepare(db, ctx)
    thread, turn = "thread-" + ctx.sid, "delegated-platform-turn"
    async with db() as session:
        repo = WorkbenchRepository(session)
        row = await repo.get_session(ctx.sid)
        sandbox = str(row["sandbox_id"])
        async with repo.transaction():
            await repo.set_session_thread(ctx.sid, thread)
            await session.execute(
                text("UPDATE workbench_sandboxes SET relay_user='u-abc' WHERE id=:id"),
                {"id": sandbox},
            )
            selected = await repo.capability_runtime_by_session(ctx.sid)
            assert selected is not None
            await repo.capability_runtime_event(
                ctx.sid, selected["runtime"]["scope_id"], turn, started=True
            )
    guard_context = (ctx.tid, ctx.sid, sandbox, thread, opened["attempt_id"])
    args = {"task": ctx.tid, "item": "document-one"}
    meta = await observed(db, guard_context, "read_input", args, turn=turn)
    await check(
        db,
        guard_context,
        "read_input",
        args,
        meta,
        resource_delegations=tuple(ctx.records),
    )
    change_catalog(ctx, change)
    with pytest.raises(CapabilityError, match="Resource delegation"):
        await check(
            db,
            guard_context,
            "read_input",
            args,
            meta,
            resource_delegations=tuple(ctx.records),
        )
    assert ctx.remote.calls == []
