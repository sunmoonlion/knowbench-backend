"""Reviewed resource evidence on real accepted calls; no formal orchestration claim."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from method_access_support import ACCESS
from sqlalchemy import text
from test_capabilities_db import CIPHER, OPERATION, OWNER, URL, Remote, approval, body
from test_execution_plan_db import open_step
from test_method_plans_db import accept, planned_attempt, task
from test_workbench_ledger_db import db as db
from tool_invocation_support import request_call

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.method_authorizations import delegation_binding
from app.application.workbench.method_capabilities import method_scope
from app.application.workbench.method_permission_recovery import recover_revoked_method
from app.domain.workbench.capabilities import (
    CapabilityError,
    SelectionPut,
    digest,
    tool_key,
)
from app.domain.workbench.method_authorization import MethodAuthorization, delegate
from app.domain.workbench.resource_delegation import ResourceDelegation
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository


async def setup(db):
    tid, sid = await task(db)
    remote: Any = Remote()
    records = []

    def service(repo):
        return Capabilities(
            repo,
            remote,
            CIPHER,
            runtime_url="http://api.test/api/mcp/capabilities",
            operations=(OPERATION,),
            resource_delegations=tuple(records),
        )

    async with db() as session:
        repo = WorkbenchRepository(session)
        asset = await service(repo).create_connection(OWNER, body())
        row = await repo.get_task(tid)
        pack = row["pack_snapshot"]
        unit = next(
            u["contract"]
            for u in pack["method_units"]
            if u["contract"]["step_id"] == "inspect"
        )
        unit["access_requirements"] = [ACCESS]
        unit["managed_capabilities"] = {
            "tools": [
                {"url": URL, "name": "send", "requirement_id": ACCESS["requirement_id"]}
            ],
            "skills": [],
        }
        async with repo.transaction():
            await session.execute(
                text(
                    "UPDATE workbench_tasks SET pack_snapshot=cast(:p as jsonb),pack_digest=:d WHERE id=:id"
                ),
                {"p": json.dumps(pack), "d": "sha256:" + digest(pack), "id": tid},
            )
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    async with db() as session:
        repo = WorkbenchRepository(session)
        scope = await method_scope(repo, OWNER, await repo.get_session(sid))
        binding = await delegation_binding(repo, scope)
        record = ResourceDelegation.model_validate(
            {
                "record_id": "mail-owner-review",
                "revision": 1,
                "issuer": "Synthetic mailbox owner",
                "owner_actor_id": OWNER,
                "binding": binding,
                "connection_id": asset["id"],
                "connection_revision": asset["revision"],
                "operation_digest": OPERATION.policy_digest,
                "requirement": ACCESS,
                "status": "active",
                "valid_from": datetime.now(UTC) - timedelta(minutes=1),
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
                "evidence": {
                    "reference": "review/mail-delegation/1",
                    "digest": "sha256:" + "a" * 64,
                },
            }
        )
        records.append(record)
    return SimpleNamespace(
        tid=tid,
        sid=sid,
        key=tool_key(asset["id"], "send"),
        remote=remote,
        record=record,
        records=records,
        service=service,
    )


async def confirm(db, ctx, *, delegated=True, reference=None):
    async with db() as session:
        svc = ctx.service(WorkbenchRepository(session))
        view = await svc.selection(OWNER, ctx.sid)
        return await svc.save_selection(
            OWNER,
            ctx.sid,
            SelectionPut(
                revision=view["revision"],
                tools=[ctx.key],
                skills=[],
                method_context=view["method"]["context"],
                purpose="Deliver the notice",
                delegations={ctx.key: reference or ctx.record.reference}
                if delegated
                else {},
            ),
        )


async def prepare(db, ctx):
    opened = await open_step(db, ctx.tid)
    async with db() as session:
        prepared = await ctx.service(WorkbenchRepository(session)).prepare(
            OWNER, ctx.sid, scene="method"
        )
    token = prepared["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
        "Authorization"
    ].split()[1]
    return opened, token


@pytest.mark.parametrize("delegated", [False, True])
async def test_explicit_confirmation_binds_one_child_and_preserves_action_approval(
    db, delegated
):
    ctx = await setup(db)
    async with db() as session:
        view = await ctx.service(WorkbenchRepository(session)).selection(OWNER, ctx.sid)
        assert view["delegations"] == {}
        assert view["method"]["delegation_options"][0][
            "authority"
        ] == ctx.record.reference.model_dump(mode="json")
    view = await confirm(db, ctx, delegated=delegated)
    assert bool(view["delegations"]) == delegated
    opened, token = await prepare(db, ctx)
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
        assert parent.grants[0].delegable == delegated
        assert child.grants[0].delegable is False
        assert child.grants[0].recipient_call_id == child.call_id
        assert child.grants[0].resource_authority == (
            ctx.record.reference if delegated else None
        )
        assert parent.expires_at <= ctx.record.expires_at if delegated else True
        with pytest.raises(CapabilityError):
            delegate(
                child,
                call_id=UUID("00000000-0000-0000-0000-000000000099"),
                method_id=child.method_id,
                requirements=child.grants,
            )
        staged = await request_call(
            ctx.service(repo), ctx.sid, token, ctx.key, {"to": "test@example.test"}
        )
        call = await repo.capability_call(OWNER, staged["call_id"])
        assert ctx.remote.calls == []
        assert call["status"] == "awaiting_approval"
    # A new service instance must recover both the grant and the exact action.
    async with db() as session:
        svc = ctx.service(WorkbenchRepository(session))
        assert (await svc.decide(OWNER, call["id"], approval(call)))[
            "status"
        ] == "succeeded"
        assert (await svc.decide(OWNER, call["id"], approval(call)))[
            "status"
        ] == "succeeded"
    assert len(ctx.remote.calls) == 1


async def test_parent_bound_step_remains_authorized_with_either_persisted_digest(db):
    from app.application.workbench.execution_plan import _meta, execution_for_task
    from app.application.workbench.method_calls import step_call, step_identity

    ctx = await setup(db)
    await confirm(db, ctx)
    await open_step(db, ctx.tid)
    async with db() as session:
        repo = WorkbenchRepository(session)
        task = await repo.get_task(ctx.tid)
        root = await repo.call_by_key(OWNER, ctx.sid, "task:" + ctx.tid)
        view = await execution_for_task(repo, task)
        meta = _meta(view, task["current_step"])
        identity, current_digest = step_identity(root["id"], meta)
        row = await repo.call_by_key(OWNER, ctx.sid, "method:" + current_digest)
        assert row["parent_call_id"] == root["id"] and row["digest"] == current_digest
        for stored in (current_digest, digest(identity)):
            async with repo.transaction():
                await session.execute(
                    text("update workbench_calls set digest=:stored where id=:id"),
                    {"stored": stored, "id": row["id"]},
                )
                assert (await step_call(repo, task, meta))["id"] == row["id"]
            prepared = await ctx.service(repo).prepare(OWNER, ctx.sid, scene="method")
            assert (
                "Authorization"
                in prepared["config"]["mcp_servers.sunmoon_capabilities"][
                    "http_headers"
                ]
            )
    assert not ctx.remote.calls


@pytest.mark.parametrize(
    "change",
    ["empty", "revoked", "digest", "owner", "binding", "connection", "expired"],
)
async def test_current_resource_record_must_cover_confirmation(db, change):
    ctx = await setup(db)
    if change == "empty":
        ctx.records.clear()
    else:
        changes = {
            "revoked": {"status": "revoked"},
            "digest": {"issuer": "Changed review"},
            "owner": {"owner_actor_id": UUID("00000000-0000-0000-0000-000000000099")},
            "binding": {
                "binding": ctx.record.binding.model_copy(
                    update={"child_method_version": "changed"}
                )
            },
            "connection": {"connection_revision": ctx.record.connection_revision + 1},
            "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        }
        ctx.records[:] = [ctx.record.model_copy(update=changes[change])]
    with pytest.raises(CapabilityError):
        await confirm(db, ctx)
    # Unselected resource evidence never upgrades or breaks an ordinary direct grant.
    view = await confirm(db, ctx, delegated=False)
    assert view["method"]["permission_status"] == "active"
    assert not ctx.remote.calls


@pytest.mark.parametrize("stage", ["prepare", "approve"])
async def test_withdrawal_is_rechecked_before_prepare_and_dispatch(db, stage):
    ctx = await setup(db)
    await confirm(db, ctx)
    if stage == "approve":
        _, token = await prepare(db, ctx)
        async with db() as session:
            repo = WorkbenchRepository(session)
            staged = await request_call(
                ctx.service(repo), ctx.sid, token, ctx.key, {"to": "test@example.test"}
            )
            call = await repo.capability_call(OWNER, staged["call_id"])
    else:
        await open_step(db, ctx.tid)
    ctx.records.clear()
    async with db() as session:
        svc = ctx.service(WorkbenchRepository(session))
        with pytest.raises(CapabilityError):
            if stage == "prepare":
                await svc.prepare(OWNER, ctx.sid, scene="method")
            else:
                await svc.decide(OWNER, call["id"], approval(call))
    assert not ctx.remote.calls


async def test_live_withdrawal_persists_stop_and_requires_explicit_reconfirmation(db):
    ctx = await setup(db)
    await confirm(db, ctx)
    opened, token = await prepare(db, ctx)
    async with db() as session:
        repo = WorkbenchRepository(session)
        staged = await request_call(
            ctx.service(repo), ctx.sid, token, ctx.key, {"to": "test@example.test"}
        )
        call = await repo.capability_call(OWNER, staged["call_id"])
    ctx.records.clear()
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = ctx.service(repo)
        await svc.refresh_method_authority((ctx.tid, opened["attempt_id"]))
        await svc.refresh_method_authority((ctx.tid, opened["attempt_id"]))
        attempt = await repo.get_attempt(opened["attempt_id"])
        assert attempt["refs"]["permission_revocation"]
        root = await repo.call_by_key(OWNER, ctx.sid, "task:" + ctx.tid)
        assert root is not None
        authority = await repo.method_authorization(OWNER, root["id"])
        assert authority is not None and authority["revoked"] is True
        selected = await repo.capability_selection(OWNER, ctx.sid)
        assert selected["runtime"]["aborted"]
        snapshot = await repo.get_task(ctx.tid)
        assert await recover_revoked_method(repo, snapshot, stopped=False) is None
        assert (
            await recover_revoked_method(repo, snapshot, stopped=True)
            == TaskState.WAITING
        )
        attempt = await repo.get_attempt(opened["attempt_id"])
        assert attempt["refs"]["usage_pending"]
        assert (await repo.capability_call(OWNER, call["id"]))["status"] == "expired"
    ctx.records.append(ctx.record)
    async with db() as session:
        view = await ctx.service(WorkbenchRepository(session)).selection(OWNER, ctx.sid)
        assert view["method"]["permission_status"] == "revoked"
    assert (await confirm(db, ctx))["method"]["permission_status"] == "active"
    assert not ctx.remote.calls


@pytest.mark.parametrize("replacement", [False, True])
async def test_old_attempt_poll_cannot_revoke_after_attempt_has_finished(
    db, replacement
):
    from app.application.workbench.ledger import Ledger
    from app.domain.workbench.states import AttemptState

    ctx = await setup(db)
    await confirm(db, ctx)
    opened, _ = await prepare(db, ctx)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await Ledger(repo).attempt_transition(
            opened["attempt_id"],
            AttemptState.FAILED,
            failure_code="synthetic_stopped_attempt",
            retryable=True,
        )
    if replacement:
        new_attempt, _ = await prepare(db, ctx)
        assert new_attempt["attempt_id"] != opened["attempt_id"]
    ctx.records.clear()
    async with db() as session:
        repo = WorkbenchRepository(session)
        before = await repo.capability_selection(OWNER, ctx.sid)
        root = await repo.call_by_key(OWNER, ctx.sid, "task:" + ctx.tid)
        assert root is not None
        authority = await repo.method_authorization(OWNER, root["id"])
        await ctx.service(repo).refresh_method_authority(
            (ctx.tid, opened["attempt_id"])
        )
        assert await repo.method_authorization(OWNER, root["id"]) == authority
        assert await repo.capability_selection(OWNER, ctx.sid) == before
        if replacement:
            attempt = await repo.get_attempt(new_attempt["attempt_id"])
            assert not attempt["refs"].get("permission_revocation")


@pytest.mark.parametrize(
    "change", ["expiry", "grant_expiry", "child_expiry", "parent_scope", "revoked"]
)
async def test_runtime_guard_rechecks_ancestors_even_while_resource_record_is_active(
    db, change
):
    ctx = await setup(db)
    await confirm(db, ctx)
    opened, _ = await prepare(db, ctx)
    async with db() as session:
        repo = WorkbenchRepository(session)
        root = await repo.call_by_key(OWNER, ctx.sid, "task:" + ctx.tid)
        assert root is not None
        target = root["id"]
        if change == "child_expiry":
            attempt = await repo.get_attempt(opened["attempt_id"])
            target = attempt["refs"]["method_call_id"]
        async with repo.transaction():
            authority = await repo.method_authorization(OWNER, target, lock=True)
            assert authority is not None
            previous = authority["revision"]
            authority["revision"] += 1
            if change in ("expiry", "child_expiry"):
                authority["expires_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
            elif change == "grant_expiry":
                authority["grants"][0]["expires_at"] = (
                    datetime.now(UTC) - timedelta(seconds=1)
                ).isoformat()
            elif change == "revoked":
                authority["revoked"] = True
            else:
                authority["grants"][0]["requirement"]["operations"] = ["read"]
            await repo.save_method_authorization(
                OWNER, ctx.sid, authority, previous=previous
            )
        await ctx.service(repo).refresh_method_authority(
            (ctx.tid, opened["attempt_id"])
        )
        attempt = await repo.get_attempt(opened["attempt_id"])
        assert attempt["refs"]["permission_revocation"]
        assert ctx.record.expires_at > datetime.now(UTC)
        assert ctx.record.status == "active"
        assert not ctx.remote.calls
