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
from tool_invocation_support import ObservedCapabilityTransport, request_call

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
        result = await svc.skill_resource(
            sid, auth, {"skill_id": first["id"], "path": "references/guide.md"}
        )
        assert result["text"] == "REFERENCE_SENTINEL"
        with pytest.raises(Forbidden):
            await svc.skill_resource(
                sid, auth, {"skill_id": other["id"], "path": "SKILL.md"}
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
            await svc.skill_resource(
                sid, auth, {"skill_id": late["id"], "path": "SKILL.md"}
            )
        new = await svc.prepare(OWNER, sid)
        assert "Newly installed" in str(new["inputs"]) and "LATE_BODY" not in str(
            new["inputs"]
        )
        with pytest.raises(Forbidden):
            await svc.skill_resource(
                sid, auth, {"skill_id": first["id"], "path": "SKILL.md"}
            )
        assert (
            await svc.skill_resource(
                sid, token(new), {"skill_id": late["id"], "path": "SKILL.md"}
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
            await svc.skill_resource(
                sid, auth, {"skill_id": a["id"], "path": "SKILL.md"}
            )
        )["text"].startswith("Read")
        await svc.action(OWNER, a["id"], AssetAction(revision=1, action="disable"))
        with pytest.raises(Forbidden):
            await svc.skill_resource(
                sid, auth, {"skill_id": a["id"], "path": "SKILL.md"}
            )
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
            await svc.skill_resource(
                sid, token(p), {"skill_id": a["id"], "path": "SKILL.md"}
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
            await svc.skill_resource(
                sid, token(before), {"skill_id": a["id"], "path": "SKILL.md"}
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
            await svc.skill_resource(
                sid, token(p), {"skill_id": a["id"], "path": "SKILL.md"}
            )
        svc.runtime_url = ""
        excluded = await svc.prepare(OWNER, sid)
        assert not excluded["config"]["mcp_servers.sunmoon_capabilities"]["enabled"]
        assert "excluded from this turn" in str(excluded["inputs"])
