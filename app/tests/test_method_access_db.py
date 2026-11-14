"""Actual method ledger, account binding and approval enforcement; no real send."""

from copy import deepcopy

import pytest
from method_access_support import ACCESS
from test_capabilities_db import OWNER, approval, service
from test_method_capabilities_db import setup_method
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.domain.workbench.capabilities import CapabilityError
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_method_scope_limits_real_approval_and_frozen_evidence(db):
    sid, _, _, _, _, _, key, token, remote = await setup_method(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo, remote)
        with pytest.raises(CapabilityError, match="confirmed method scope"):
            await request_call(svc, sid, token, key, {"to": "outside@example.test"})
        assert not await repo.capability_calls(OWNER, sid)
        assert not remote.calls
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        call = await repo.capability_call(OWNER, staged["call_id"])
        assert (
            call["detail"]["access"]["requirement"]["requirement_id"]
            == "deliver-notice"
        )
        assert call["detail"]["access"]["requirement"]["resources"][0]["values"] == [
            "test@example.test"
        ]
        await svc.decide(OWNER, call["id"], approval(call))
        assert len(remote.calls) == 1


async def test_missing_saved_confirmation_is_not_reconstructed_from_declaration(db):
    sid, _, _, _, _, _, _, _, remote = await setup_method(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo, remote)
        async with repo.transaction():
            selected = await repo.capability_selection(OWNER, sid, lock=True)
            detail = selected["detail"]
            context = selected["runtime"]["method"]["context"]
            del detail["method_selections"][context]["access"]
            await repo.capability_save_selection(
                OWNER, sid, selected["revision"], detail
            )
        view = await svc.selection(OWNER, sid)
        assert any(i["code"] == "method_permission_unconfirmed" for i in view["issues"])
        with pytest.raises(CapabilityError):
            await svc.prepare(OWNER, sid, scene="method")
        assert not remote.calls


async def test_method_cannot_bind_a_technical_tool_with_another_business_permission(db):
    access = deepcopy(ACCESS)
    access["capability_ref"] = "unrelated-mailbox-read"
    with pytest.raises(CapabilityError, match="differs"):
        await setup_method(db, access=access)


async def test_changed_permission_invalidates_pending_approval_and_discovery(db):
    sid, _, _, _, _, _, key, token, remote = await setup_method(db)
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = service(repo, remote)
        staged = await request_call(svc, sid, token, key, {"to": "test@example.test"})
        call = await repo.capability_call(OWNER, staged["call_id"])
        async with repo.transaction():
            selected = await repo.capability_selection(OWNER, sid, lock=True)
            runtime = selected["runtime"]
            del runtime["tools"][key]["access"]
            await repo.capability_runtime(OWNER, sid, runtime)
        assert key not in svc.runtime_tools(runtime)
        with pytest.raises(CapabilityError, match="permission.*(changed|no longer covers)"):
            await svc.decide(OWNER, call["id"], approval(call))
        assert (await repo.capability_call(OWNER, call["id"]))[
            "status"
        ] == "awaiting_approval"
        assert not remote.calls
        await svc.decide(OWNER, call["id"], approval(call, decision="decline"))
