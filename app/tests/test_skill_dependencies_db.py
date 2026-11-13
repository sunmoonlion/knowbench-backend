import asyncio
import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from test_capabilities_db import URL, body, service
from test_capability_routes_db import application
from test_workbench_ledger_db import OTHER, OWNER, seed
from test_workbench_ledger_db import db as db

from app.domain.workbench.capabilities import (
    AssetAction,
    CapabilityError,
    SelectionPut,
    SkillCreate,
    tool_key,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_install_before_connecting_then_bind_current_account_and_revoke(db):
    _, _, sid = await seed(db)
    async with db() as s:
        svc = service(WorkbenchRepository(s))
        skill = await svc.create_skill(
            OWNER,
            SkillCreate(
                request_id="portable-skill",
                name="notice",
                description="Send a notice",
                version="1",
                content="Review before sending",
                tool_requirements=[{"url": URL, "name": "send"}],
            ),
        )
        assert (await svc.assets(OWNER))[0]["detail"]["dependency_status"][0][
            "status"
        ] == "connection_required"
        assert await svc.assets(OTHER) == []
        # Auto discovery includes guidance without granting any connection tools.
        prepared = await svc.prepare(OWNER, sid)
        assert (
            '"unresolved_connection_dependencies": 1' in prepared["inputs"][0]["text"]
        )
        assert "candidates" not in prepared["inputs"][0]["text"]
        token = prepared["config"]["mcp_servers.sunmoon_capabilities"]["http_headers"][
            "Authorization"
        ].split()[1]
        resource = await svc.skill_resource(
            sid, token, {"skill_id": skill["id"], "path": "SKILL.md"}
        )
        assert resource["connection_dependencies"][0]["status"] == "connection_required"
        assert resource["connection_dependencies"][0]["selected_tools"] == []
        selection = await svc.selection(OWNER, sid)
        with pytest.raises(CapabilityError):
            await svc.save_selection(
                OWNER,
                sid,
                SelectionPut(revision=selection["revision"], skills=[skill["id"]]),
            )
        first = await svc.create_connection(OWNER, body("portable-first-account"))
        second = await svc.create_connection(OWNER, body("portable-second-account"))
        output = os.getenv("CAPABILITY_UI_OUT")
        if output:
            target = Path(output)
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
            async with AsyncClient(
                transport=ASGITransport(application(db, svc.remote)),
                base_url="http://testserver",
            ) as http:
                for name, path in (
                    ("connector-catalog", "/api/workbench/capabilities"),
                    (
                        "connector-selection",
                        f"/api/workbench/sessions/{sid}/capabilities",
                    ),
                ):
                    response = await http.get(path)
                    assert response.status_code == 200
                    await asyncio.to_thread(
                        (target / (name + ".json")).write_text,
                        json.dumps(response.json(), ensure_ascii=False, indent=2),
                    )
        second_key = tool_key(second["id"], "send")
        selected = await svc.save_selection(
            OWNER,
            sid,
            SelectionPut(
                revision=selection["revision"], skills=[skill["id"]], tools=[second_key]
            ),
        )
        dependency = selected["available_skills"][0]["detail"]["dependency_status"][0]
        assert dependency["status"] == "selected" and len(dependency["candidates"]) == 2
        prepared = await svc.prepare(OWNER, sid)
        enabled = prepared["config"]["mcp_servers.sunmoon_capabilities"][
            "enabled_tools"
        ]
        assert second_key in enabled and tool_key(first["id"], "send") not in enabled
        await svc.action(
            OWNER,
            second["id"],
            AssetAction(revision=second["revision"], action="disable"),
        )
        degraded = await svc.prepare(OWNER, sid)
        assert (
            degraded["config"]["mcp_servers.sunmoon_capabilities"]["enabled"] is False
        )
        assert "excluded from this turn" in degraded["inputs"][0]["text"]
        # Presence of the first account never makes it an implicit replacement.
        current = await svc.selection(OWNER, sid)
        assert current["tools"] == [second_key]
