"""Real admitted versions used in normal conversations, with synthetic MCP only."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_capabilities_db import OWNER, approval
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_method_calls import FormalMethodCalls
from app.application.workbench.formal_methods import FormalMethods
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import Forbidden, IdempotencyConflict, NotFound
from app.domain.workbench.formal_method_calls import (
    FormalMethodCallRequest,
    FormalMethodInvocation,
)
from app.domain.workbench.method_authorization import MethodAuthorization
from app.infrastructure.workbench.repository import WorkbenchRepository


async def setup_registered(db, *, kind="chat"):
    from formal_method_use_support import admitted

    frozen, asset, remote, factory, sid = await admitted(db, kind=kind)
    body = FormalMethodCallRequest(
        request_id=uuid4(),
        reference=frozen.reference,
        values={"to": "test@example.test"},
    )
    return factory, remote, sid, body, frozen, asset


@pytest.mark.parametrize("kind", ["chat", "work"])
async def test_explicit_formal_use_has_real_root_and_exact_approval_without_task_or_turn(
    db, kind
):
    factory, remote, sid, body, frozen, _ = await setup_registered(db, kind=kind)
    async with db() as s:
        repo = WorkbenchRepository(s)
        caps = factory(repo)
        svc = FormalMethodCalls(caps)
        catalog = await svc.catalog(OWNER, sid)
        assert any(
            item["reference"] == frozen.reference.model_dump(mode="json")
            and item["available"]
            for item in catalog["items"]
        )
        requested = await svc.request(OWNER, sid, body)
        FormalMethodInvocation.model_validate(requested)
        assert requested["status"] == "awaiting_approval" and not remote.calls
        child = requested["call"]
        assert child["detail"]["execution_kind"] == "formal_method"
        assert (
            not {"runtime", "selection_revision", "method", "formal_method"}
            & child["detail"].keys()
        )
        root = await repo.call_by_key(
            OWNER, sid, "formal-session:" + str(body.request_id)
        )
        raw_child = await repo.capability_call(OWNER, child["id"])
        assert root["kind"] == "method" and raw_child["parent_call_id"] == root["id"]
        assert root["parent_call_id"] is None
        assert all(
            row["task_id"] is None and row["attempt_id"] is None
            for row in (root, raw_child)
        )
        interaction = await repo.get_interaction(child["interaction_id"])
        assert interaction["task_id"] is None and interaction["attempt_id"] is None
        assert await repo.method_authorization(OWNER, root["id"]) is None
        result = await caps.decide(OWNER, child["id"], approval(child))
        assert result["status"] == "succeeded"
        final = await svc.status(OWNER, sid, body.request_id)
        FormalMethodInvocation.model_validate(final)
        assert final["status"] == "succeeded" and final["outputs"] == {
            "receipt": "expected-never-sent"
        }
        auth = MethodAuthorization.model_validate(
            await repo.method_authorization(OWNER, root["id"])
        )
        assert auth.method_id == str(frozen.reference.method_id)
        assert (
            not auth.grants[0].delegable
            and str(auth.grants[0].recipient_call_id) == root["id"]
        )
        assert auth.grants[0].confirmation_context == child["digest"]
        assert await svc.request(OWNER, sid, body) == final
        assert await svc.decide(OWNER, child["id"], approval(child)) == final
        assert await svc.list_calls(OWNER, sid) == [final]
        assert len(remote.calls) == 1
        session = await repo.get_session(sid)
        assert (
            session["wheel"] == "user"
            and session["active_task_id"] is None
            and session["thread_id"] is None
        )
        assert not await repo.list_attempts(str(root["id"]))
        events = await repo.list_events(session_id=sid)
        assert not any(
            e["type"].startswith(("task/", "turn/", "thread/", "item/", "wheel/"))
            for e in events
        )


async def test_concurrent_retries_create_one_approval_and_dispatch(db):
    factory, remote, sid, body, _, _ = await setup_registered(db)

    async def request():
        async with db() as s:
            return await FormalMethodCalls(factory(WorkbenchRepository(s))).request(
                OWNER, sid, body
            )

    requests = await asyncio.gather(request(), request(), return_exceptions=True)
    assert not [r for r in requests if isinstance(r, BaseException)], requests
    one, two = requests
    assert (
        one["method_call_id"] == two["method_call_id"]
        and one["call"]["id"] == two["call"]["id"]
    )

    async def decide():
        async with db() as s:
            return await FormalMethodCalls(factory(WorkbenchRepository(s))).decide(
                OWNER, one["call"]["id"], approval(one["call"])
            )

    decisions = await asyncio.gather(decide(), decide(), return_exceptions=True)
    assert not [r for r in decisions if isinstance(r, BaseException)], decisions
    assert all(d["status"] in ("dispatching", "succeeded") for d in decisions)
    assert len(remote.calls) == 1


@pytest.mark.parametrize("field", ["values", "reference"])
async def test_same_request_identity_cannot_change_work(db, field):
    factory, remote, sid, body, _, _ = await setup_registered(db)
    async with db() as s:
        svc = FormalMethodCalls(factory(WorkbenchRepository(s)))
        await svc.request(OWNER, sid, body)
        changed = body.model_dump(mode="json")
        if field == "values":
            changed[field]["to"] = "different@example.test"
        else:
            changed[field]["version"] = 2
        with pytest.raises(IdempotencyConflict):
            await svc.request(
                OWNER, sid, FormalMethodCallRequest.model_validate(changed)
            )
        assert not remote.calls


async def test_identical_inputs_with_new_user_request_are_separate_actions(db):
    factory, remote, sid, body, _, _ = await setup_registered(db)
    async with db() as s:
        svc = FormalMethodCalls(factory(WorkbenchRepository(s)))
        first = await svc.request(OWNER, sid, body)
        second = await svc.request(
            OWNER, sid, body.model_copy(update={"request_id": uuid4()})
        )
        assert first["method_call_id"] != second["method_call_id"]
        await svc.decide(OWNER, first["call"]["id"], approval(first["call"]))
        await svc.decide(OWNER, second["call"]["id"], approval(second["call"]))
        assert len(remote.calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "policy",
        "license",
        "archived",
        "wheel",
        "arguments",
        "root_reference",
        "revoked",
    ],
)
async def test_approval_rechecks_exact_current_authority(db, change):
    factory, remote, sid, body, _, asset = await setup_registered(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        caps = factory(repo)
        svc = FormalMethodCalls(caps)
        requested = await svc.request(OWNER, sid, body)
        child = requested["call"]
        if change == "policy":
            from app.domain.workbench.tool_operations import OperationCatalog

            caps.operations = OperationCatalog(())
            svc = FormalMethodCalls(caps)
        elif change == "license":
            caps.formal_sources = FormalMethods(repo, source_licenses=())
            svc = FormalMethodCalls(caps)
        else:
            async with repo.transaction():
                if change == "source":
                    await s.execute(
                        text(
                            "UPDATE workbench_capability_assets SET revision=revision+1 WHERE id=:id"
                        ),
                        {"id": asset["id"]},
                    )
                elif change == "archived":
                    await repo.archive_session(sid, archived=True)
                elif change == "wheel":
                    await s.execute(
                        text(
                            "UPDATE workbench_sessions SET wheel='runtime' WHERE id=:id"
                        ),
                        {"id": sid},
                    )
                elif change == "arguments":
                    raw = await repo.capability_call(OWNER, child["id"])
                    raw["detail"]["arguments"] = {"to": "different@example.test"}
                    await repo.update_call(
                        OWNER, child["id"], raw["status"], raw["detail"]
                    )
                elif change == "root_reference":
                    root = await repo.call_by_key(
                        OWNER, sid, "formal-session:" + str(body.request_id)
                    )
                    root["detail"]["request"]["reference"]["version"] = 2
                    await repo.update_call(
                        OWNER, root["id"], root["status"], root["detail"]
                    )
                elif change == "revoked":
                    auth = MethodAuthorization(
                        call_id=requested["method_call_id"],
                        revision=1,
                        parent_call_id=None,
                        parent_revision=None,
                        method_id=str(body.reference.method_id),
                        revoked=True,
                        expires_at=datetime.now(UTC) + timedelta(minutes=5),
                        grants=(),
                    )
                    await repo.save_method_authorization(
                        OWNER, sid, auth.model_dump(mode="json"), previous=0
                    )
        with pytest.raises((CapabilityError, Forbidden)):
            await svc.decide(OWNER, child["id"], approval(child))
        assert not remote.calls
        assert (await repo.get_interaction(child["interaction_id"]))[
            "status"
        ] == "pending"


async def test_unknown_outcome_is_retained_without_replay(db):
    factory, remote, sid, body, _, _ = await setup_registered(db)
    remote.fail = True
    async with db() as s:
        svc = FormalMethodCalls(factory(WorkbenchRepository(s)))
        requested = await svc.request(OWNER, sid, body)
        result = await svc.decide(
            OWNER, requested["call"]["id"], approval(requested["call"])
        )
        assert result["status"] == "unknown" and result["outputs"] is None
        assert await svc.request(OWNER, sid, body) == result
        assert (
            await svc.decide(
                OWNER, requested["call"]["id"], approval(requested["call"])
            )
            == result
        )
        assert len(remote.calls) == 1


@pytest.mark.parametrize("returned", [{"unexpected": True}, {"receipt": "x" * 64000}])
async def test_typed_failure_retains_raw_external_success(db, monkeypatch, returned):
    factory, remote, sid, body, _, _ = await setup_registered(db)
    original = remote.call

    async def invalid(*args):
        result = await original(*args)
        return {**result, "structuredContent": returned}

    monkeypatch.setattr(remote, "call", invalid)
    async with db() as s:
        svc = FormalMethodCalls(factory(WorkbenchRepository(s)))
        requested = await svc.request(OWNER, sid, body)
        result = await svc.decide(
            OWNER, requested["call"]["id"], approval(requested["call"])
        )
        FormalMethodInvocation.model_validate(result)
        assert result["status"] == "failed" and result["call"]["status"] == "succeeded"
        assert result["outputs"] is None and "not rolled back" in result["detail"]


async def test_owner_only_history_and_current_session_gate(db):
    factory, remote, sid, body, _, _ = await setup_registered(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = FormalMethodCalls(factory(repo))
        requested = await svc.request(OWNER, sid, body)
        with pytest.raises(NotFound):
            await svc.request(OTHER, sid, body)
        with pytest.raises(NotFound):
            await svc.status(OTHER, sid, body.request_id)
        with pytest.raises(NotFound):
            await svc.decide(
                OTHER, requested["call"]["id"], approval(requested["call"])
            )
        await svc.decide(
            OWNER, requested["call"]["id"], approval(requested["call"], "decline")
        )
        async with repo.transaction():
            await repo.archive_session(sid, archived=True)
        assert (await svc.status(OWNER, sid, body.request_id))["status"] == "declined"
        assert (await svc.request(OWNER, sid, body))["status"] == "declined"
        with pytest.raises(CapabilityError):
            await svc.request(
                OWNER, sid, body.model_copy(update={"request_id": uuid4()})
            )
        assert not remote.calls


async def test_restart_preserves_host_approval_but_its_own_expiry_blocks_dispatch(db):
    factory, remote, sid, body, _, _ = await setup_registered(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = FormalMethodCalls(factory(repo))
        requested = await svc.request(OWNER, sid, body)
        session = await repo.get_session(sid)
        async with repo.transaction():
            assert not await repo.expire_pending_tool_approvals(
                sandbox_id=str(session["sandbox_id"]), reason="model runner restarted"
            )
        assert (await svc.status(OWNER, sid, body.request_id))[
            "status"
        ] == "awaiting_approval"
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_interactions SET expires_at=now()-interval '1 minute' WHERE id=:id"
                ),
                {"id": requested["call"]["interaction_id"]},
            )
        expired = await svc.decide(
            OWNER, requested["call"]["id"], approval(requested["call"])
        )
        assert expired["status"] == "expired" and not remote.calls


async def test_lost_committed_dispatch_recovers_unknown_without_authority_refresh(
    db, monkeypatch
):
    factory, remote, sid, body, _, asset = await setup_registered(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        caps = factory(repo)
        svc = FormalMethodCalls(caps)
        requested = await svc.request(OWNER, sid, body)

        async def lost(*args):
            raise RuntimeError("lost after committed dispatch")

        monkeypatch.setattr(caps, "_dispatch_call", lost)
        with pytest.raises(RuntimeError, match="lost after"):
            await svc.decide(
                OWNER, requested["call"]["id"], approval(requested["call"])
            )
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_calls SET updated_at=now()-interval '3 minutes' WHERE id=:id"
                ),
                {"id": requested["call"]["id"]},
            )
            await s.execute(
                text(
                    "UPDATE workbench_capability_assets SET status='disabled' WHERE id=:id"
                ),
                {"id": asset["id"]},
            )
        recovered = await FormalMethodCalls(factory(repo)).request(OWNER, sid, body)
        assert recovered["status"] == "unknown" and not remote.calls


async def test_incubation_needs_no_listing_but_normal_use_requires_live_listing(
    db, monkeypatch
):
    from formal_method_use_support import TestListings, admitted

    frozen, _, remote, factory, sid = await admitted(db, purge=False)
    body = FormalMethodCallRequest(
        request_id=uuid4(),
        reference=frozen.reference,
        values={"to": "test@example.test"},
    )
    async with db() as session:
        caps = factory(WorkbenchRepository(session))
        review = await caps.formal_sources.self_review_outputs(
            "author", frozen.reference
        )
        assert review["samples"] and review["samples"][0]["steps"][0]["artifact"]
        svc = FormalMethodCalls(caps)
        caps.formal_market = TestListings([])
        assert await svc.catalog(OWNER, sid) == {"items": []}
        with pytest.raises(Forbidden):
            await svc.request(OWNER, sid, body)
        caps.formal_market = TestListings([frozen])
        waiting = await svc.request(OWNER, sid, body)
        child = waiting["call"]
        caps.formal_market = TestListings([])
        with pytest.raises(Forbidden):
            await svc.decide(OWNER, child["id"], approval(child))
        assert not remote.calls

        caps.formal_market = TestListings([frozen])
        before_send = caps._check_before_send

        async def withdraw_then_check(*args):
            caps.formal_market.versions.clear()
            await before_send(*args)

        monkeypatch.setattr(caps, "_check_before_send", withdraw_then_check)
        failed = await svc.decide(OWNER, child["id"], approval(child))
        assert failed["status"] == "failed" and not remote.calls


async def test_self_review_samples_for_registered_versions_outlive_temporary_case_files(
    db,
):
    from datetime import UTC, datetime, timedelta

    from formal_method_use_support import admitted

    from app.application.workbench.test_runs import KnowbotTestConductor
    from app.bootstrap.workbench import workbench_stores

    frozen, _, _, factory, _ = await admitted(db)
    async with db() as session:
        service = factory(WorkbenchRepository(session)).formal_sources
        before = await service.self_review_outputs("author", frozen.reference)
        assert before["samples"]
        future = datetime.now(UTC) + timedelta(days=31)
        await KnowbotTestConductor(workbench_stores(db), now=lambda: future).cleanup()
        assert await service.self_review_outputs("author", frozen.reference) == before
