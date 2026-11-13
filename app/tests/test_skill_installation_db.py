"""HTTP installation, account ownership, immutable versions and uninstall revocation."""

import asyncio

from httpx import ASGITransport, AsyncClient
from test_capabilities_db import service
from test_capability_routes_db import application
from test_skill_installation import COMMIT, archive
from test_workbench_ledger_db import db as db
from test_workbench_ledger_db import seed

from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import capability_routes as routes


class Source:
    async def fetch(self, url, commit=None):
        assert commit in (None, COMMIT)
        return {
            "repository": "https://github.com/owner/repo",
            "commit": COMMIT,
            "prefix": "",
            "archive": archive(),
        }


async def test_preview_install_retry_then_remove_and_scope_rejection(db):
    _, _, sid = await seed(db)
    app = application(db, None)

    async def configured():
        async with db() as s:
            svc = service(WorkbenchRepository(s))
            svc.skill_source = Source()
            yield svc

    app.dependency_overrides[routes.service] = configured
    base = "/api/workbench/capabilities/skill-sources"
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        preview = await http.post(
            base + "/preview", json={"url": "https://github.com/owner/repo"}
        )
        assert preview.status_code == 200, preview.text
        source = preview.json()
        import os
        from pathlib import Path

        if os.environ.get("CAPABILITY_UI_OUT"):
            out = Path(os.environ["CAPABILITY_UI_OUT"])
            await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
            (out / "source-preview.json").write_text(preview.text)
        candidate = source["candidates"][0]
        body = {
            "request_id": "install-source-one",
            "url": source["repository"],
            "commit": source["commit"],
            "path": candidate["path"],
            "digest": candidate["digest"],
        }
        tampered = await http.post(base + "/install", json={**body, "digest": "b" * 64})
        assert tampered.status_code == 409
        installed = await http.post(base + "/install", json=body)
        assert installed.status_code == 201, installed.text
        if os.environ.get("CAPABILITY_UI_OUT"):
            await asyncio.to_thread(
                (
                    Path(os.environ["CAPABILITY_UI_OUT"]) / "source-installed.json"
                ).write_text,
                installed.text,
            )
        asset = installed.json()
        assert asset["detail"]["source"]["commit"] == COMMIT
        assert len(asset["detail"]["files"]) == 3
        assert "resources" not in asset["detail"]
        again = await http.post(base + "/install", json=body)
        assert again.json()["id"] == asset["id"]
        selection = (
            await http.get(f"/api/workbench/sessions/{sid}/capabilities")
        ).json()
        assert asset["id"] in selection["skills"]
        removed = await http.post(
            f"/api/workbench/capabilities/{asset['id']}/actions",
            json={"action": "remove", "revision": asset["revision"]},
        )
        assert removed.status_code == 200 and removed.json()["status"] == "disconnected"
        selection = (
            await http.get(f"/api/workbench/sessions/{sid}/capabilities")
        ).json()
        assert asset["id"] not in selection["skills"]
        cannot_enable = await http.post(
            f"/api/workbench/capabilities/{asset['id']}/actions",
            json={"action": "enable", "revision": removed.json()["revision"]},
        )
        assert cannot_enable.status_code == 409
        # A fresh installation creates a new account asset while preserving old history.
        reinstall = await http.post(
            base + "/install", json={**body, "request_id": "install-source-two"}
        )
        assert reinstall.status_code == 201 and reinstall.json()["id"] != asset["id"]
