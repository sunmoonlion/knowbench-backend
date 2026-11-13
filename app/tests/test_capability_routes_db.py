"""Exercise the actual router, response schemas and scoped MCP protocol."""

import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from test_capabilities_db import OTHER, OWNER, approval, prepared, service
from test_workbench_ledger_db import db as db
from test_workbench_routes import principal

from app.bootstrap.api import create_app
from app.domain.workbench.capabilities import (
    ActionDecision,
    AssetAction,
    ConnectionCreate,
    ConnectionRenew,
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
        transport=ASGITransport(app), base_url="http://testserver"
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
    ConnectionRenew,
    SkillCreate,
    SkillPackageCreate,
    AssetAction,
    SelectionPut,
    ActionDecision,
    Asset,
    Catalog,
    Selection,
    Call,
    Calls,
)


def test_provider_schema_export_is_current():
    artifact = json.loads((ROOT / "contracts/capabilities-v1.json").read_text())
    assert artifact == {m.__name__: m.model_json_schema() for m in MODELS}
