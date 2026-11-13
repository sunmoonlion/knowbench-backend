"""Actual database and HTTP: method bindings, lazy Skill reads and approval suspension."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_capabilities_db import OWNER, URL, Remote, approval, body, service
from test_capability_routes_db import application
from test_workbench_ledger_db import db as db
from test_workbench_ledger_db import handover, seed

from app.application.workbench.ledger import Ledger
from app.domain.workbench.capabilities import (
    AssetAction,
    CapabilityError,
    SelectionPut,
    SkillCreate,
    digest,
    tool_key,
)
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.packs import SMOKE
from app.domain.workbench.states import TaskState
from app.infrastructure.workbench.repository import WorkbenchRepository


async def setup_method(db):
    _, _, sid = await seed(db)
    remote = Remote()
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        connection = await svc.create_connection(OWNER, body())
        key = tool_key(connection["id"], "send")
        skill = await svc.create_skill(
            OWNER,
            SkillCreate(
                request_id="method-skill-1",
                name="notice",
                version="1",
                description="Create a notice",
                content="METHOD_SKILL_BODY",
                dependencies=[key],
            ),
        )
        unrelated = await svc.create_skill(
            OWNER,
            SkillCreate(
                request_id="method-skill-2",
                name="unrelated",
                version="1",
                description="Unrelated",
                content="NOT_FOR_THIS_STEP",
            ),
        )
        task_id = (await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER))[
            "task_id"
        ]
        pack = SMOKE.model_dump(mode="json")
        pack["workflow"][0]["managed_capabilities"] = {
            "tools": [{"url": URL, "name": "send"}],
            "skills": [
                {
                    "name": skill["name"],
                    "version": "1",
                    "digest": skill["detail"]["digest"],
                }
            ],
        }
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_tasks SET pack_source='knowmart', pack_snapshot=cast(:p as jsonb), pack_digest=:d WHERE id=:id"
                ),
                {"p": json.dumps(pack), "d": "sha256:" + digest(pack), "id": task_id},
            )
        selected = await svc.selection(OWNER, sid)
        assert selected["editable"] and selected["method"]["managed_bindings_supported"]
        assert selected["issues"][0]["code"] == "method_bindings_required"
        assert [a["id"] for a in selected["available_skills"]] == [skill["id"]]
        with pytest.raises(CapabilityError):
            await svc.prepare(OWNER, sid, scene="method")
        with pytest.raises(CapabilityError):
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=selected["revision"],
                    tools=[key],
                    skills=[unrelated["id"]],
                    method_context=selected["method"]["context"],
                ),
            )
        saved = await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selected["revision"],
                tools=[key],
                skills=[skill["id"]],
                method_context=selected["method"]["context"],
                purpose="Deliver the declared notice",
            ),
        )
        assert not saved["issues"]
        led = Ledger(repo)
        await led.transition(task_id, TaskState.VALIDATING)
        await led.transition(task_id, TaskState.QUEUED)
        attempt = await led.open_attempt(
            task_id, step_id=pack["workflow"][0]["step_id"], step_version="1"
        )
        prepared = await svc.prepare(OWNER, sid, scene="method")
        assert "METHOD_SKILL_BODY" not in str(prepared["inputs"])
        assert "NOT_FOR_THIS_STEP" not in str(prepared)
        config = prepared["config"]["mcp_servers.sunmoon_capabilities"]
        assert set(config["enabled_tools"]) == {
            key,
            "action_result",
            "skill_read",
            "skill_files",
        }
        assert config["tool_timeout_sec"] == 960
        token = config["http_headers"]["Authorization"].split()[1]
    return (
        sid,
        task_id,
        attempt["attempt_id"],
        connection,
        skill,
        unrelated,
        key,
        token,
        remote,
    )


async def test_step_scope_lazy_read_and_cancellation(db):
    sid, tid, aid, conn, skill, other, key, token, remote = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        result = await svc.skill_resource(
            sid, token, {"skill_id": skill["id"], "path": "SKILL.md"}
        )
        assert "METHOD_SKILL_BODY" in str(result)
        with pytest.raises(Forbidden):
            await svc.skill_resource(
                sid, token, {"skill_id": other["id"], "path": "SKILL.md"}
            )
        selection = await svc.selection(OWNER, sid)
        assert not selection["editable"]
        with pytest.raises(CapabilityError):
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(
                    revision=selection["revision"],
                    method_context=selection["method"]["context"],
                ),
            )
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_tasks SET cancel_requested_at=now() WHERE id=:t"
                ),
                {"t": tid},
            )
        with pytest.raises(Forbidden):
            await svc.skill_resource(
                sid, token, {"skill_id": skill["id"], "path": "SKILL.md"}
            )
        with pytest.raises(Forbidden):
            await svc.request_call(sid, token, key, {"to": "test@example.test"})
        assert remote.calls == []


@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_http_method_tool_waits_for_decision_and_returns_real_result(
    db, decision
):
    sid, tid, aid, conn, skill, other, key, token, remote = await setup_method(db)
    app = application(db, remote)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        pending = asyncio.create_task(
            http.post(
                f"/api/mcp/capabilities/{sid}",
                headers={"Authorization": "Bearer " + token},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": key, "arguments": {"to": "test@example.test"}},
                },
            )
        )
        try:
            for _ in range(100):
                async with db() as s:
                    repo = WorkbenchRepository(s)
                    calls = await repo.capability_calls(OWNER, sid)
                if calls:
                    break
                await asyncio.sleep(0.02)
            assert calls and not pending.done() and not remote.calls
            async with db() as s:
                assert (await WorkbenchRepository(s).get_task(tid))[
                    "state"
                ] == TaskState.WAITING
            answer = await http.post(
                f"/api/workbench/capability-calls/{calls[0]['id']}/decision",
                json=approval(calls[0], decision).model_dump(),
            )
            assert answer.status_code == 200, answer.text
            response = await asyncio.wait_for(pending, 5)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            message = json.loads(
                next(
                    line[6:]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                )
            )
            result = message["result"]
            assert result["isError"] == (decision == "decline")
            assert len(remote.calls) == (1 if decision == "accept" else 0)
            async with db() as s:
                assert (await WorkbenchRepository(s).get_task(tid))[
                    "state"
                ] == TaskState.RUNNING
            again = await http.post(
                f"/api/workbench/capability-calls/{calls[0]['id']}/decision",
                json=approval(calls[0], decision).model_dump(),
            )
            assert again.status_code == 200 and len(remote.calls) == (
                1 if decision == "accept" else 0
            )
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_step_approval_expiry_and_revoke_before_dispatch(db):
    sid, tid, aid, conn, skill, other, key, token, remote = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo, remote)
        requested = await svc.request_call(sid, token, key, {"to": "test@example.test"})
        call = await repo.capability_call(OWNER, requested["call_id"])
        await svc.action(OWNER, conn["id"], AssetAction(revision=1, action="disable"))
        with pytest.raises(CapabilityError):
            await svc.decide(OWNER, call["id"], approval(call))
        assert not remote.calls
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_interactions SET expires_at=now()-interval '1 second' WHERE id=:i"
                ),
                {"i": call["interaction_id"]},
            )
        assert (await svc.method_call_result(sid, token, call["id"]))[
            "status"
        ] == "expired"
        assert (await repo.get_task(tid))["state"] == TaskState.RUNNING


async def test_step_transition_rejects_old_scope(db):
    sid, tid, aid, conn, skill, other, key, token, remote = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_tasks SET current_step=current_step+1 WHERE id=:t"
                ),
                {"t": tid},
            )
        with pytest.raises(Forbidden):
            await service(repo, remote).runtime(sid, token)


async def test_dynamic_plan_step_inherits_requirements_after_planning_finishes(db):
    from test_execution_plan_db import open_step
    from test_method_plans_db import accept, planned_attempt, task

    tid, sid = await task(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo)
        skill = await svc.create_skill(
            OWNER,
            SkillCreate(
                request_id="dynamic-skill",
                name="inspect",
                version="1",
                description="Inspection",
                content="DYNAMIC_STEP_GUIDE",
            ),
        )
        snapshot = (await repo.get_task(tid))["pack_snapshot"]
        next(
            unit
            for unit in snapshot["method_units"]
            if unit["contract"]["step_id"] == "inspect"
        )["contract"]["managed_capabilities"] = {
            "tools": [],
            "skills": [
                {"name": "inspect", "version": "1", "digest": skill["detail"]["digest"]}
            ],
        }
        async with repo.transaction():
            await s.execute(
                text(
                    "UPDATE workbench_tasks SET pack_snapshot=cast(:p as jsonb), pack_digest=:d WHERE id=:id"
                ),
                {
                    "p": json.dumps(snapshot),
                    "d": "sha256:" + digest(snapshot),
                    "id": tid,
                },
            )
    aid, version = await planned_attempt(db, tid)
    await accept(db, tid, aid, version)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo)
        assert await svc.hold_method(OWNER, sid)
        waiting = await repo.get_task(tid)
        assert waiting["state"] == TaskState.WAITING
        # A binding pause must not open a billable execution attempt.
        assert len(await repo.list_attempts(tid)) == 1
        selected = await svc.selection(OWNER, sid)
        assert selected["editable"] and selected["method"]["step_id"] == "inspect_1"
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selected["revision"],
                skills=[skill["id"]],
                method_context=selected["method"]["context"],
            ),
        )
        events = await repo.list_events(session_id=sid)
        event = next(e for e in reversed(events) if e["type"] == "interaction/opened")
        await Ledger(repo).respond_interaction(
            str(waiting["active_interaction_id"]),
            token=event["payload"]["token"],
            response={"decision": "continue"},
            owner_actor_id=OWNER,
        )
        assert not await svc.hold_method(OWNER, sid)
    opened = await open_step(db, tid)
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        prepared = await svc.prepare(OWNER, sid, scene="method")
        token = prepared["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
        assert (await svc.runtime(sid, token))["runtime"]["method"][
            "attempt_id"
        ] == opened["attempt_id"]
        assert "DYNAMIC_STEP_GUIDE" in str(
            await svc.skill_resource(
                sid, token, {"skill_id": skill["id"], "path": "SKILL.md"}
            )
        )


async def test_author_catalog_is_owned_active_and_excludes_secrets_and_bodies(db):
    from app.application.workbench.test_runs import KnowbotTestService

    sid, tid, aid, conn, skill, other, key, token, remote = await setup_method(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            await s.execute(
                text(
                    "INSERT INTO auth_user (id, issuer, subject, roles, scopes) VALUES (:id, 'https://identity.test', 'author', '[]', '[]')"
                ),
                {"id": OWNER},
            )
        catalog = await KnowbotTestService(repo).author_capabilities("author")
        assert catalog["tools"][0]["name"] == "send"
        assert len(catalog["skills"]) == 2
        notice = next(item for item in catalog["skills"] if item["name"] == "notice")
        assert notice["required_tools"] == [{"url": URL, "name": "send"}]
        encoded = json.dumps(catalog)
        assert (
            "METHOD_SKILL_BODY" not in encoded
            and "only-server-knows" not in encoded
            and conn["id"] not in encoded
        )
        assert await KnowbotTestService(repo).author_capabilities("other-author") == {
            "tools": [],
            "skills": [],
        }
        await service(repo, remote).action(
            OWNER,
            skill["id"],
            AssetAction(action="disable", revision=skill["revision"]),
        )
        assert [
            v["name"]
            for v in (await KnowbotTestService(repo).author_capabilities("author"))[
                "skills"
            ]
        ] == ["unrelated"]
        with pytest.raises(Forbidden):
            await service(repo, remote).runtime(
                "00000000-0000-4000-8000-000000000000", "unknown"
            )
