import asyncio
import hashlib
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from test_auth_routes_security import FakeAuthService, session
from test_capabilities_db import OTHER, OWNER, service
from test_formal_method_use_routes_db import application
from test_workbench_ledger_db import db as db
from test_workbench_routes import principal

from app.interfaces.http.middleware import auth


@pytest.mark.parametrize(
    "kind,content", [("script", "print(1)\n"), ("instruction", "逐行核对。\n")]
)
async def test_author_registers_without_skill_or_incubation(
    db, monkeypatch, kind, content
):
    app = application(db, service)
    del app.dependency_overrides[auth.get_web_current_user]
    browser = session("web", "profile:read").model_copy(
        update={"principal": principal(OWNER)}
    )
    other = browser.model_copy(update={"principal": principal(OTHER)})
    monkeypatch.setattr(
        auth, "web_auth_service", FakeAuthService({"author": browser, "other": other})
    )
    base = "/api/workbench/registry/sources"
    sid = str(uuid4())
    path = f"{base}/{sid}/1"
    draft = {"name": "reconcile", "kind": kind, "content": content}
    digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as client:
        assert (await client.get(base)).status_code == 401
        client.cookies.set(auth._web_profile.session_cookie_name, "author")
        headers = {
            "X-CSRF-Token": browser.csrf_token,
            "Origin": next(iter(auth._web_profile.frontend_origins)),
        }
        assert (await client.put(path, json=draft)).status_code == 403
        r1, r2 = await asyncio.gather(
            *(client.put(path, json=draft, headers=headers) for _ in range(2))
        )
        assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
        assert (
            r1.json()
            == r2.json()
            == {
                "source_id": sid,
                "version": 1,
                "name": "reconcile",
                "kind": kind,
                "digest": digest,
            }
        )
        assert (await client.get(base)).json() == [r1.json()]
        assert (await client.get("/api/workbench/registry")).json()["formal"] == []
        assert (await client.get("/api/workbench/registry")).json()["installed"] == []
        edit = {**draft, "content": "updated\n"}
        assert (await client.put(path, json=edit, headers=headers)).status_code == 409
        assert (
            await client.put(f"{base}/{sid}/3", json=edit, headers=headers)
        ).status_code == 409
        assert (
            await client.put(f"{base}/{sid}/2", json=edit, headers=headers)
        ).status_code == 200
        assert (await client.put(path, json=draft, headers=headers)).json() == r1.json()
        got = await client.get(path, params={"digest": digest})
        assert (
            got.json()["content"] == content
            and "no-store" in got.headers["cache-control"]
        )
        assert (
            await client.get(path, params={"digest": "sha256:" + "0" * 64})
        ).status_code == 404
        client.cookies.set(auth._web_profile.session_cookie_name, "other")
        assert (await client.get(base)).json() == []
        assert (await client.get(path, params={"digest": digest})).status_code == 404
