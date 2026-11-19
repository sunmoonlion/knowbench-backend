"""Installed Skills are discovered lazily, with per-turn versions and revocation."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from test_capabilities_db import OTHER, OWNER, service
from test_capability_routes_db import application
from test_skill_package import archive
from test_workbench_ledger_db import db as db
from test_workbench_ledger_db import seed
from tool_invocation_support import (
    ObservedCapabilityTransport,
    read_skill,
    request_call,
)

from app.domain.workbench.capabilities import (
    AssetAction,
    SelectionPut,
    SkillCreate,
)
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.skill_package import SkillPackageCreate
from app.infrastructure.workbench.repository import WorkbenchRepository


def package(request="package-request-1"):
    return SkillPackageCreate(
        request_id=request,
        name="guidance",
        description="Design Agently workflows",
        version="commit-1",
        archive_base64=archive(),
    )


def token(prepared):
    return prepared["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
        "Authorization"
    ].split()[1]


async def test_auto_discovery_late_install_lazy_reads_and_owner_isolation(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo)
        assert (await svc.selection(OWNER, sid))["skill_mode"] == "auto"
        first = await svc.create_skill(OWNER, package(), package=True)
        assert "resources" not in first["detail"] and len(first["detail"]["files"]) == 3
        assert (await svc.create_skill(OWNER, package(), package=True))["id"] == first[
            "id"
        ]
        other = await svc.create_skill(OTHER, package(), package=True)
        prepared = await svc.prepare(OWNER, sid)
        assert "Design Agently workflows" in str(prepared["inputs"])
        assert "Read references/guide.md" not in str(prepared["inputs"])
        assert "REFERENCE_SENTINEL" not in str(prepared["inputs"])
        assert other["id"] not in str(prepared)
        assert set(
            prepared["config"]["mcp_servers.sunmoon_capabilities"]["enabled_tools"]
        ) == {"skill_read", "skill_files"}
        auth = token(prepared)
        result = await read_skill(
            svc, sid, auth, {"skill_id": first["id"], "path": "references/guide.md"}
        )
        assert result["text"] == "REFERENCE_SENTINEL"
        with pytest.raises(Forbidden):
            await read_skill(
                svc, sid, auth, {"skill_id": other["id"], "path": "SKILL.md"}
            )
        late = await svc.create_skill(
            OWNER,
            SkillCreate(
                request_id="late-instructions",
                name="late",
                description="Newly installed",
                version="1",
                content="LATE_BODY",
            ),
        )
        with pytest.raises(Forbidden):
            await read_skill(
                svc, sid, auth, {"skill_id": late["id"], "path": "SKILL.md"}
            )
        new = await svc.prepare(OWNER, sid)
        assert "Newly installed" in str(new["inputs"]) and "LATE_BODY" not in str(
            new["inputs"]
        )
        with pytest.raises(Forbidden):
            await read_skill(
                svc, sid, auth, {"skill_id": first["id"], "path": "SKILL.md"}
            )
        assert (
            await read_skill(
                svc, sid, token(new), {"skill_id": late["id"], "path": "SKILL.md"}
            )
        )["text"] == "LATE_BODY"


async def test_revocation_expiry_recovery_and_explicit_scope(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo)
        a = await svc.create_skill(OWNER, package(), package=True)
        auth = token(await svc.prepare(OWNER, sid))
    # A new service/repository instance reconstructs the scope from PostgreSQL.
    async with db() as s:
        repo = WorkbenchRepository(s)
        svc = service(repo)
        assert (
            await read_skill(svc, sid, auth, {"skill_id": a["id"], "path": "SKILL.md"})
        )["text"].startswith("Read")
        await svc.action(OWNER, a["id"], AssetAction(revision=1, action="disable"))
        with pytest.raises(Forbidden):
            await read_skill(svc, sid, auth, {"skill_id": a["id"], "path": "SKILL.md"})
        assert (await svc.prepare(OWNER, sid))["inputs"] == []
        await svc.action(OWNER, a["id"], AssetAction(revision=2, action="enable"))
        p = await svc.prepare(OWNER, sid)
        selected = await repo.capability_selection(OWNER, sid)
        expired = {
            **selected["runtime"],
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        async with repo.transaction():
            await repo.capability_runtime(OWNER, sid, expired)
        with pytest.raises(Forbidden):
            await read_skill(
                svc, sid, token(p), {"skill_id": a["id"], "path": "SKILL.md"}
            )
        before = await svc.prepare(OWNER, sid)
        await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selected["revision"], skill_mode="explicit", skills=[]
            ),
        )
        with pytest.raises(Forbidden):
            await read_skill(
                svc, sid, token(before), {"skill_id": a["id"], "path": "SKILL.md"}
            )
        assert (await svc.prepare(OWNER, sid))["config"][
            "mcp_servers.sunmoon_capabilities"
        ]["enabled"] is False


async def test_http_package_import_and_lazy_mcp_read(db):
    _, _, sid = await seed(db)
    app = application(db, None)
    async with AsyncClient(
        transport=ObservedCapabilityTransport(app, db), base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/api/workbench/capabilities/skill-packages", json=package().model_dump()
        )
        assert response.status_code == 201, response.text
        asset = response.json()
        assert "resources" not in asset["detail"]
        async with db() as s:
            prepared = await service(WorkbenchRepository(s)).prepare(OWNER, sid)
        headers = {"Authorization": "Bearer " + token(prepared)}
        url = f"/api/mcp/capabilities/{sid}"
        tools = (
            await http.post(
                url, headers=headers, json={"id": 1, "method": "tools/list"}
            )
        ).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"skill_read", "skill_files"}
        for t in tools:
            assert t["annotations"]["readOnlyHint"] is True
        result = (
            await http.post(
                url,
                headers=headers,
                json={
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "skill_read",
                        "arguments": {
                            "skill_id": asset["id"],
                            "path": "references/guide.md",
                        },
                    },
                },
            )
        ).json()
        assert (
            json.loads(result["result"]["content"][0]["text"])["text"]
            == "REFERENCE_SENTINEL"
        )
        bad = await http.post(
            "/api/workbench/capabilities/skill-packages",
            json={**package().model_dump(), "archive_base64": "bad"},
        )
        assert bad.status_code == 422 and "archive_base64" not in bad.text
        large = await http.post(
            "/api/workbench/capabilities/skill-packages",
            content=b"x" * (3 * 1024 * 1024 + 1),
        )
        assert large.status_code == 413


async def test_skill_discovery_never_grants_tool_dependencies(db):
    _, _, sid = await seed(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        # No tool can be introduced by text in an archive.
        a = await svc.create_skill(OWNER, package(), package=True)
        p = await svc.prepare(OWNER, sid)
        with pytest.raises(Forbidden):
            await request_call(svc, sid, token(p), "shell", {"command": "echo unsafe"})
        frozen = await svc.prepare(OWNER, sid, scene="method")
        assert (
            frozen["inputs"] == []
            and not frozen["config"]["mcp_servers.sunmoon_capabilities"]["enabled"]
        )
        with pytest.raises(Forbidden):
            await read_skill(
                svc, sid, token(p), {"skill_id": a["id"], "path": "SKILL.md"}
            )
        svc.runtime_url = ""
        excluded = await svc.prepare(OWNER, sid)
        assert not excluded["config"]["mcp_servers.sunmoon_capabilities"]["enabled"]
        assert "excluded from this turn" in str(excluded["inputs"])


async def test_runner_reuses_mcp_subscription_and_refreshes_turn_scope(db):
    from test_workbench_runner_db import FakeAppServer, _started, wait_for

    from app.application.workbench.session_service import SessionService

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sandbox, sid, thread = await _started(db, fake)
        runner.capabilities = service
        async with db() as session:
            svc = service(WorkbenchRepository(session))
            skill = await svc.create_skill(OWNER, package(), package=True)

        scopes = []
        for index in range(3):
            request_id = f"reuse-{index}"
            async with db() as session:
                repo = WorkbenchRepository(session)
                if index == 2:
                    await service(repo).action(
                        OWNER, skill["id"], AssetAction(revision=1, action="disable")
                    )
                await SessionService(repo).record_user_turn_requested(
                    sid, owner_actor_id=OWNER, text="hello", request_id=request_id
                )
                async with repo.transaction():
                    await repo.enqueue_command(
                        session_id=sid,
                        sandbox_id=sandbox,
                        kind="turn.start",
                        payload={
                            "text": "hello",
                            "request_id": request_id,
                            "by": "user",
                        },
                    )
            assert await runner.run_once() == 1
            scope = runner.links[sandbox].capability_scopes[thread]

            async def completed(scope=scope):
                async with db() as session:
                    selected = await WorkbenchRepository(
                        session
                    ).capability_runtime_by_session(sid)
                    runtime = selected["runtime"]
                    return (
                        runtime["scope_id"] == scope
                        and runtime["status"] == "completed"
                    )

            assert await wait_for(completed)
            scopes.append(scope)
            resumes = [
                r["params"] for r in fake.requests if r["method"] == "thread/resume"
            ]
            assert (
                len(resumes)
                == sum(r["method"] == "thread/unsubscribe" for r in fake.requests)
                == (1 if index < 2 else 2)
            )
            assert resumes[-1]["config"]["mcp_servers.sunmoon_capabilities"][
                "enabled"
            ] is (index < 2)
        assert len(set(scopes)) == 3
    finally:
        await fake.close()


@pytest.mark.parametrize("listing", [False, True])
async def test_skill_read_cannot_follow_a_turn_change_while_waiting_for_lock(
    db, monkeypatch, listing
):
    import asyncio

    from tool_invocation_support import observe_capability_call

    _, _, sid = await seed(db)
    async with db() as session:
        svc = service(WorkbenchRepository(session))
        skill = await svc.create_skill(OWNER, package(), package=True)
        before = await svc.prepare(OWNER, sid)
        gateway = token(before)
        args = {"skill_id": skill["id"]}
        if not listing:
            args["path"] = "references/guide.md"
        name = "skill_files" if listing else "skill_read"
        old = await observe_capability_call(svc, sid, name, args)
        waiting, resume = asyncio.Event(), asyncio.Event()
        lock = svc._locked_session

        async def pause(owner, session_id):
            waiting.set()
            await resume.wait()
            return await lock(owner, session_id)

        monkeypatch.setattr(svc, "_locked_session", pause)
        pending = asyncio.create_task(
            svc.skill_resource(sid, gateway, args, meta=old, listing=listing)
        )
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            async with db() as other:
                repo = WorkbenchRepository(other)
                other_svc = service(repo)
                async with repo.transaction():
                    await repo.capability_runtime_event(
                        sid,
                        before["scope_id"],
                        old["x-codex-turn-metadata"]["turn_id"],
                        started=False,
                    )
                replacement = await other_svc.create_skill(
                    OWNER,
                    SkillCreate(
                        request_id="replacement-skill",
                        name="replacement",
                        version="1",
                        description="Second turn only",
                        content="SECOND_TURN_BODY",
                    ),
                )
                await other_svc.save_selection(
                    OWNER,
                    sid,
                    SelectionPut(
                        revision=before["revision"], skills=[replacement["id"]]
                    ),
                )
                after = await other_svc.prepare(OWNER, sid, previous_gateway=gateway)
                assert (
                    after["config"] == before["config"]
                    and after["scope_id"] != before["scope_id"]
                )
                retired = await observe_capability_call(other_svc, sid, name, args)
                with pytest.raises(Forbidden, match="outside the selected scope"):
                    await other_svc.skill_resource(
                        sid, gateway, args, meta=retired, listing=listing
                    )
                current_args = {"skill_id": replacement["id"]}
                if not listing:
                    current_args["path"] = "SKILL.md"
                new = await observe_capability_call(other_svc, sid, name, current_args)
                resource = await other_svc.skill_resource(
                    sid, gateway, current_args, meta=new, listing=listing
                )
                assert (
                    resource["skill_id"] == replacement["id"]
                    if listing
                    else resource["text"] == "SECOND_TURN_BODY"
                )
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise
        finally:
            resume.set()
        with pytest.raises(Forbidden, match="another execution turn"):
            await asyncio.wait_for(pending, 5)


async def test_delayed_runner_ack_cannot_bind_replacement_skill_scope(db):
    from test_workbench_runner_db import FakeAppServer, _started

    from app.domain.workbench.projects import ExecutionScene

    fake = FakeAppServer()
    await fake.start()
    try:
        runner, sandbox, sid, thread = await _started(db, fake)
        runner.capabilities = service
        link = runner.links[sandbox]
        async with db() as session:
            svc = service(WorkbenchRepository(session))
            skill = await svc.create_skill(OWNER, package(), package=True)
        await link.prepare_capabilities(thread, scene=ExecutionScene.CONVERSATION)
        first = link.capability_scopes[thread]
        await link.prepare_capabilities(thread, scene=ExecutionScene.CONVERSATION)
        replacement = link.capability_scopes[thread]
        await link.record_capability_turn(thread, "late-ack", first)
        async with db() as session:
            repo = WorkbenchRepository(session)
            current = (await repo.capability_runtime_by_session(sid))["runtime"]
            assert (
                current["scope_id"] == replacement and current["status"] == "prepared"
            )
            assert "turn_id" not in current
        await link.record_capability_turn(thread, "replacement", replacement)
        async with db() as session:
            svc = service(WorkbenchRepository(session))
            current = (await svc.repo.capability_runtime_by_session(sid))["runtime"]
            assert (
                current["turn_id"] == "replacement" and current["status"] == "applied"
            )
            gateway = token({"config": link.capability_config[thread]})
            value = await read_skill(
                svc,
                sid,
                gateway,
                {"skill_id": skill["id"], "path": "references/guide.md"},
            )
            assert value["text"] == "REFERENCE_SENTINEL"
    finally:
        await fake.close()
