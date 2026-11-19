"""Resource-owner consent at the web, dispatch, recovery and seal boundaries."""

import asyncio
import json

import pytest
from formal_plan_support import advance
from formal_recursive_support import setup_recursive
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_capabilities_db import approval
from test_formal_execution_db import approve
from test_formal_method_use_routes_db import application
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db
from test_workbench_routes import principal

from app.application.workbench.artifact_read_authority import EVENT
from app.domain.workbench.errors import Forbidden
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.http.middleware import auth


async def artifact_action(db):
    ctx = await setup_recursive(db)
    initial = await advance(db, ctx)
    await approve(db, ctx.request, ctx.factory, initial)
    return ctx, await advance(db, ctx)


async def test_owner_consent_and_intermediate_withdrawal_survive_reconnect(
    db, monkeypatch
):
    ctx, pending = await artifact_action(db)
    action = pending["actions"][0]
    from app.domain.workbench.errors import TestManagedInKnowlab
    from app.interfaces.endpoints import capability_routes as routes

    app = application(db, ctx.factory)
    del app.dependency_overrides[routes.method_read_service]
    del app.dependency_overrides[routes.service]

    async def db_session():
        async with db() as session:
            yield session

    app.dependency_overrides[routes.get_db_session] = db_session
    monkeypatch.setattr(routes, "capability_service", ctx.factory)
    async with db() as session:
        browser = WorkbenchRepository(session, browser_view=True)
        assert pending["cases"][0]["task_id"] not in {
            str(t["id"]) for t in await browser.list_tasks(owner_actor_id=ctx.owner)
        }
        with pytest.raises(TestManagedInKnowlab):
            await browser.get_task(
                pending["cases"][0]["task_id"], owner_actor_id=ctx.owner
            )
    url = "/api/workbench/capability-calls/" + action["call_id"] + "/artifact-read"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        preview = await http.get(url)
        assert preview.status_code == 200, preview.text
        assert preview.headers["cache-control"] == "no-store"
        data = preview.json()
        assert data["authority"] is None
        binding = data["binding"]
        path = binding["path"]
        assert len(path) == 3
        assert binding["fields"]["to"]["producer_path"] == ["first", "first"]
        assert binding["fields"]["to"]["output"] == "receipt"
        consent = {
            "scope_digest": data["scope_digest"],
            "expires_at": data["expires_at"],
        }
        app.dependency_overrides[auth.get_web_current_user] = lambda: principal(OTHER)
        assert (await http.post(url, json=consent)).status_code in (403, 404)
        app.dependency_overrides[auth.get_web_current_user] = lambda: principal(
            ctx.owner
        )
        assert (
            await http.post(url, json={**consent, "scope_digest": "0" * 64})
        ).status_code == 403
        replies = await asyncio.gather(
            http.post(url, json=consent), http.post(url, json=consent)
        )
        assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
        assert replies[0].json() == replies[1].json()
        grants = replies[0].json()["authority"]["grants"]
        assert [g["call_id"] for g in grants] == [p["call_id"] for p in path]
        assert [g["remaining_depth"] for g in grants] == [2, 1, 0]
        assert grants[-1]["recipient_call_id"] is None
        body = {"scope_digest": consent["scope_digest"], "retain": []}
        middle = url + "/" + path[1]["call_id"]
        result = await http.put(middle, json=body)
        assert result.status_code == 200, result.text
        assert [g["fields"] for g in result.json()["authority"]["grants"]] == [
            ["to"],
            [],
            [],
        ]
        assert (
            await http.put(middle, json={**body, "retain": ["to"]})
        ).status_code == 403
        assert (await http.post(url, json=consent)).status_code == 403
        decision = {
            "decision": "accept",
            "subject_digest": action["subject_digest"],
            "token": action["token"],
        }
        refused = await http.post(
            url.removesuffix("/artifact-read") + "/decision", json=decision
        )
        assert refused.status_code == 403 and "outside" in refused.text
    with pytest.raises(Forbidden):
        await approve(db, ctx.request, ctx.factory, pending)
    assert len(ctx.remote.calls) == 1
    async with db() as session:
        repo = WorkbenchRepository(session)
        call = await repo.capability_call(ctx.owner, action["call_id"])
        assert call["status"] == "awaiting_approval"
        assert (
            len(await repo.list_task_events(str(call["task_id"]), types=(EVENT,))) == 2
        )


async def test_committed_artifact_action_is_never_repeated_after_worker_loss(
    db, monkeypatch
):
    ctx, pending = await artifact_action(db)
    call_id = pending["actions"][0]["call_id"]
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = ctx.factory(repo)
        call = await repo.capability_call(ctx.owner, call_id)
        tid, aid = str(call["task_id"]), str(call["attempt_id"])

        async def lost(_owner, _call, asset, intent, _reader):
            await ctx.remote.call(
                asset["detail"]["url"],
                None,
                intent["tool"]["name"],
                intent["arguments"],
            )
            raise RuntimeError("lost after dispatch commit")

        monkeypatch.setattr(svc, "_dispatch_call", lost)
        with pytest.raises(RuntimeError, match="lost after dispatch"):
            await svc.decide(ctx.owner, call_id, approval(call))
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            events = await repo.list_task_events(tid, types=(EVENT,))
            assert len(events) == 1 and events[0]["payload"]["call_id"] == call_id
            await session.execute(
                text(
                    "UPDATE workbench_calls SET updated_at=now()-interval '3 minutes' WHERE id=:id"
                ),
                {"id": call_id},
            )

    async def recover():
        async with db() as session:
            svc = ctx.factory(WorkbenchRepository(session))
            return await svc.request_host_call(ctx.owner, tid, aid)

    results = await asyncio.gather(recover(), recover())
    assert [(r["id"], r["status"]) for r in results] == [(call_id, "unknown")] * 2
    async with db() as session:
        svc = ctx.factory(WorkbenchRepository(session))
        assert (await svc.decide(ctx.owner, call_id, approval(call)))[
            "status"
        ] == "unknown"
    assert len(ctx.remote.calls) == 2


async def test_sealing_rejects_a_replaced_resource_consent(db):
    from test_formal_recursive_evidence_db import collected, finished, update_event

    ctx, evidence = await finished(db)
    resource = evidence["steps"][2]["governance"]["artifact_read"]
    assert resource["binding"]["fields"]["to"]["producer_path"] == ["first"]
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            events = await repo.list_task_events(ctx.result["task_id"], types=(EVENT,))
            selected = next(
                e
                for e in events
                if e["payload"]["scope_digest"] == resource["scope_digest"]
            )
            selected["payload"]["binding"]["fields"]["to"]["version"] += 1
            await update_event(session, selected)
    broken = await collected(db, ctx)
    assert not broken["validated"]
    assert "artifact_read_approval_mismatch" in str(broken["errors"])


async def test_legacy_pending_action_requires_explicit_supplement(db):
    from app.domain.workbench.formal_host import host_intent_digest

    ctx, pending = await artifact_action(db)
    call_id = pending["actions"][0]["call_id"]
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            call = await repo.capability_call(ctx.owner, call_id, lock=True)
            detail = dict(call["detail"])
            detail.pop("artifact_read_digest")
            old_digest = host_intent_digest(detail)
            await session.execute(
                text(
                    "UPDATE workbench_calls SET digest=:digest, detail=cast(:detail as jsonb) WHERE id=:id"
                ),
                {"id": call_id, "digest": old_digest, "detail": json.dumps(detail)},
            )
            await session.execute(
                text(
                    "UPDATE workbench_interactions SET subject_digest=:digest, prompt=prompt #- '{subject,artifact_read}' WHERE id=:id"
                ),
                {"id": call["interaction_id"], "digest": old_digest},
            )
    app = application(db, ctx.factory)
    url = "/api/workbench/capability-calls/" + call_id + "/artifact-read"
    pending["actions"][0]["subject_digest"] = old_digest
    with pytest.raises(Forbidden):
        await approve(db, ctx.request, ctx.factory, pending)
    assert len(ctx.remote.calls) == 1
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        shown = await http.get(url)
        assert shown.status_code == 200, shown.text
        scope = shown.json()
        allowed = await http.post(
            url,
            json={
                "scope_digest": scope["scope_digest"],
                "expires_at": scope["expires_at"],
            },
        )
        assert allowed.status_code == 200, allowed.text
    await approve(db, ctx.request, ctx.factory, pending)
    assert len(ctx.remote.calls) == 2
