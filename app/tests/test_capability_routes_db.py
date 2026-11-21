"""Exercise the actual router, response schemas and scoped MCP protocol."""

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from test_capabilities_db import OTHER, OWNER, approval, prepared, service
from test_workbench_ledger_db import db as db
from test_workbench_routes import principal
from tool_invocation_support import ObservedCapabilityTransport

from app.bootstrap.api import create_app
from app.domain.workbench.capabilities import (
    ActionDecision,
    AssetAction,
    ConnectionCreate,
    ConnectionRenew,
    MethodPermissionRevoke,
    SelectionPut,
    SkillCreate,
)
from app.domain.workbench.capability_contract import (
    Asset,
    Call,
    Calls,
    Catalog,
    Selection,
)
from app.domain.workbench.connection_oauth import OAuthStart
from app.domain.workbench.formal_method_calls import (
    FormalMethodCallRequest,
    FormalMethodCatalogResponse,
    FormalMethodInvocation,
    FormalMethodInvocations,
)
from app.domain.workbench.skill_installation import (
    SkillInstall,
    SkillSourcePreview,
    SkillSourceRequest,
)
from app.domain.workbench.skill_package import SkillPackageCreate
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import capability_routes as routes
from app.interfaces.http.middleware.auth import get_web_current_user
from core.config import Settings


def application(db, remote):
    app = create_app(Settings())

    async def capability_service():
        async with db() as s:
            yield service(WorkbenchRepository(s), remote)

    app.dependency_overrides[routes.service] = capability_service
    app.dependency_overrides[routes.require_workbench_enabled] = lambda: None
    app.dependency_overrides[get_web_current_user] = lambda: principal(OWNER)
    return app


async def test_http_contract_authentication_approval_and_mcp_scope(db):
    sid, asset, key, token, remote = await prepared(db)
    app = application(db, remote)
    async with AsyncClient(
        transport=ObservedCapabilityTransport(app, db), base_url="http://testserver"
    ) as http:
        assert (await http.get("/api/workbench/capabilities")).status_code == 200
        catalog = (await http.get("/api/workbench/capabilities")).json()
        assert "only-server-knows" not in json.dumps(catalog)
        assert catalog["assets"][0]["detail"]["tools"][0]["key"] == key
        selection = (
            await http.get(f"/api/workbench/sessions/{sid}/capabilities")
        ).json()
        assert Selection.model_validate(selection)
        url = f"/api/mcp/capabilities/{sid}"
        assert (
            await http.post(url, json={"id": 1, "method": "initialize"})
        ).status_code == 401
        auth = {"Authorization": "Bearer " + token}
        inventory = (
            await http.post(url, headers=auth, json={"id": 1, "method": "tools/list"})
        ).json()["result"]["tools"]
        assert {t["name"] for t in inventory} == {key, "action_result"}
        request = {
            "id": 2,
            "method": "tools/call",
            "params": {"name": key, "arguments": {"to": "r@example.test"}},
        }
        staged = (await http.post(url, headers=auth, json=request)).json()
        call_id = json.loads(staged["result"]["content"][0]["text"])["call_id"]
        listed = (
            await http.get(f"/api/workbench/sessions/{sid}/capability-calls")
        ).json()
        call = listed["calls"][0]
        assert call["id"] == call_id and not remote.calls
        from app.infrastructure.storage.postgres import get_db_session

        async def route_session():
            async with db() as session:
                yield session

        app.dependency_overrides[get_db_session] = route_session
        async with db() as session:
            stored = await WorkbenchRepository(session).capability_call(OWNER, call_id)
        url_decision = f"/api/workbench/interactions/{stored['interaction_id']}/respond"
        invalid = {"token": "x" * 32}
        app.dependency_overrides[get_web_current_user] = lambda: principal(OTHER)
        assert (await http.post(url_decision, json=invalid)).status_code == 404
        app.dependency_overrides[get_web_current_user] = lambda: principal(OWNER)
        assert (await http.post(url_decision, json=invalid)).status_code == 422
        done = await http.post(
            f"/api/workbench/capability-calls/{call_id}/decision",
            json=approval(call).model_dump(),
        )
        assert done.status_code == 200 and done.json()["status"] == "succeeded"
        assert len(remote.calls) == 1
        result = (
            await http.post(
                url,
                headers=auth,
                json={
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "action_result",
                        "arguments": {"call_id": call_id},
                    },
                },
            )
        ).json()
        assert (
            json.loads(result["result"]["content"][0]["text"])["status"] == "succeeded"
        )
        async with db() as session:
            reads = await WorkbenchRepository(session).list_events(session_id=sid)
            [evidence] = [
                e["payload"] for e in reads if e["type"] == "capability/resource_read"
            ]
            assert evidence["resource"] == {"call_id": call_id}
            assert evidence["registered_source"]["name"] == "action_result"
            assert evidence["invocation"]["call_id"]
        app.dependency_overrides[get_web_current_user] = lambda: principal(OTHER)
        assert (
            await http.get(f"/api/workbench/sessions/{sid}/capabilities")
        ).status_code == 404
        assert (
            await http.post(
                f"/api/workbench/capability-calls/{call_id}/decision",
                json=approval(call).model_dump(),
            )
        ).status_code == 404


ROOT = Path(__file__).resolve().parents[1]
MODELS = (
    ConnectionCreate,
    OAuthStart,
    ConnectionRenew,
    SkillCreate,
    SkillPackageCreate,
    SkillSourceRequest,
    SkillSourcePreview,
    SkillInstall,
    AssetAction,
    SelectionPut,
    ActionDecision,
    MethodPermissionRevoke,
    Asset,
    Catalog,
    Selection,
    Call,
    Calls,
    FormalMethodCallRequest,
    FormalMethodCatalogResponse,
    FormalMethodInvocation,
    FormalMethodInvocations,
)


def test_provider_schema_export_is_current():
    artifact = json.loads((ROOT / "contracts/capabilities-v1.json").read_text())
    assert artifact == {m.__name__: m.model_json_schema() for m in MODELS}


@pytest.mark.parametrize("name", ["skill_files", "skill_read", "action_result"])
async def test_withdrawn_helper_disappears_from_active_gateway_and_cannot_read(
    db, name
):
    from sqlalchemy import text
    from test_skill_package_db import package, token
    from test_workbench_ledger_db import seed

    if name == "action_result":
        sid, _, _, gateway_token, remote = await prepared(db)
        args = {"call_id": sid}
    else:
        remote = None
        _, _, sid = await seed(db)
        async with db() as session:
            svc = service(WorkbenchRepository(session), remote)
            skill = await svc.create_skill(OWNER, package(), package=True)
            scope = await svc.prepare(OWNER, sid)
        gateway_token = token(scope)
        args = {"skill_id": skill["id"]}
        if name == "skill_read":
            args["path"] = "SKILL.md"
    url = f"/api/mcp/capabilities/{sid}"
    headers = {"Authorization": "Bearer " + gateway_token}
    async with AsyncClient(
        transport=ObservedCapabilityTransport(application(db, remote), db),
        base_url="http://testserver",
    ) as http:
        listing = {"id": 1, "method": "tools/list"}
        before = (await http.post(url, headers=headers, json=listing)).json()
        assert name in {t["name"] for t in before["result"]["tools"]}
        async with db() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM workbench_registry_service_functions WHERE provider='knowbench' AND name=:name AND version=1"
                ),
                {"name": name},
            )
        after = (await http.post(url, headers=headers, json=listing)).json()
        assert name not in {t["name"] for t in after["result"]["tools"]}
        response = (
            await http.post(
                url,
                headers=headers,
                json={
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
            )
        ).json()
        assert "registration changed" in str(response)
