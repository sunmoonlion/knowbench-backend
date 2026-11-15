"""Browser boundary for real admitted methods; only the remote MCP is synthetic."""

import json
from uuid import uuid4

import pytest
from formal_method_use_support import admitted
from httpx import ASGITransport, AsyncClient
from test_auth_routes_security import FakeAuthService, session
from test_capabilities_db import OWNER, approval
from test_workbench_ledger_db import OTHER
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_routes import principal

from app.bootstrap.api import create_app
from app.domain.workbench.formal_method_calls import (
    FormalMethodCatalogResponse,
    FormalMethodInvocation,
    FormalMethodInvocations,
)
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import capability_routes as routes
from app.interfaces.http.middleware import auth
from core.config import Settings


def application(db, factory):
    app = create_app(Settings())

    async def service():
        async with db() as s:
            yield factory(WorkbenchRepository(s))

    app.dependency_overrides[routes.service] = service
    app.dependency_overrides[routes.require_workbench_enabled] = lambda: None
    app.dependency_overrides[auth.get_web_current_user] = lambda: principal(OWNER)
    return app


@pytest.mark.parametrize("kind", ["chat", "work"])
async def test_browser_method_use_and_history_have_scoped_typed_results(db, kind):
    frozen, _, remote, factory, sid = await admitted(db, kind=kind)
    app = application(db, factory)
    base = f"/api/workbench/sessions/{sid}"
    body = {
        "request_id": str(uuid4()),
        "reference": frozen.reference.model_dump(mode="json"),
        "values": {"to": "test@example.test"},
    }
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        catalog = await http.get(base + "/formal-methods")
        assert catalog.status_code == 200
        assert "no-store" in catalog.headers["cache-control"]
        parsed = FormalMethodCatalogResponse.model_validate(catalog.json())
        assert len(parsed.items) == 1 and parsed.items[0].available
        assert "expected-never-sent" not in catalog.text
        requested = await http.post(base + "/formal-method-calls", json=body)
        assert requested.status_code == 200, requested.text
        invocation = FormalMethodInvocation.model_validate(requested.json())
        assert invocation.status == "awaiting_approval" and not remote.calls
        call = requested.json()["call"]
        serialized = json.dumps(call)
        assert all(
            key not in serialized
            for key in (
                "frozen",
                '"formal_method":',
                "authorization",
                "selection_revision",
                "expected-never-sent",
            )
        )
        decision = await http.post(
            f"/api/workbench/capability-calls/{call['id']}/decision",
            json=approval(call).model_dump(),
        )
        assert decision.status_code == 200 and decision.json()["status"] == "succeeded"
        path = base + "/formal-method-calls/" + body["request_id"]
        result = await http.get(path)
        assert result.status_code == 200, result.text
        typed = FormalMethodInvocation.model_validate(result.json())
        assert typed.outputs == {"receipt": "expected-never-sent"}
        assert typed.status == "succeeded" and len(remote.calls) == 1
        assert (
            await http.post(base + "/formal-method-calls", json=body)
        ).json() == result.json()
        history = await http.get(base + "/formal-method-calls")
        assert FormalMethodInvocations.model_validate(history.json()).calls == [typed]
        assert "no-store" in history.headers["cache-control"]
        assert (
            await http.get(
                f"/api/workbench/sessions/{uuid4()}/formal-method-calls/{body['request_id']}"
            )
        ).status_code == 404
        app.dependency_overrides[auth.get_web_current_user] = lambda: principal(OTHER)
        for suffix in (
            "/formal-methods",
            "/formal-method-calls",
            "/formal-method-calls/" + body["request_id"],
        ):
            assert (await http.get(base + suffix)).status_code == 404
        assert (
            await http.post(base + "/formal-method-calls", json=body)
        ).status_code == 404
        assert (
            await http.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json=approval(call).model_dump(),
            )
        ).status_code == 404
        assert len(remote.calls) == 1


async def test_browser_auth_csrf_and_strict_request_cannot_be_bypassed(db, monkeypatch):
    frozen, _, remote, factory, sid = await admitted(db)
    app = application(db, factory)
    del app.dependency_overrides[auth.get_web_current_user]
    browser = session("web", "profile:read")
    browser = browser.model_copy(update={"principal": principal(OWNER)})
    monkeypatch.setattr(
        auth, "web_auth_service", FakeAuthService({"owner-session": browser})
    )
    path = f"/api/workbench/sessions/{sid}/formal-method-calls"
    body = {
        "request_id": str(uuid4()),
        "reference": frozen.reference.model_dump(mode="json"),
        "values": {"to": "test@example.test"},
    }
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://testserver"
    ) as http:
        assert (await http.get(path)).status_code == 401
        assert (
            await http.post(
                path,
                json=body,
                headers={
                    "Authorization": "Bearer service-identity-is-not-browser-auth"
                },
            )
        ).status_code == 401
        http.cookies.set(auth._web_profile.session_cookie_name, "owner-session")
        assert (await http.post(path, json=body)).status_code == 403
        headers = {
            "X-CSRF-Token": browser.csrf_token,
            "Origin": next(iter(auth._web_profile.frontend_origins)),
        }
        assert (
            await http.post(
                path, json={**body, "owner_subject": OTHER}, headers=headers
            )
        ).status_code == 422
        assert (
            await http.post(
                path, json={**body, "values": {"to": "x" * 70000}}, headers=headers
            )
        ).status_code == 422
        response = await http.post(path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert not remote.calls
