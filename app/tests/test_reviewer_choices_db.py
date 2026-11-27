"""A reusable preference has no authority to dispatch or disclose a credential."""

from uuid import uuid4

import httpx
from test_formal_method_use_routes_db import application
from test_workbench_ledger_db import OTHER, OWNER
from test_workbench_ledger_db import db as db
from test_workbench_routes import principal

from app.bootstrap.workbench import reviewer_choices
from app.domain.workbench.review_routes import ReviewGrant
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import workbench_routes as routes
from app.interfaces.http.middleware.auth import get_web_current_user
from core.config import Settings


async def configured(db, patch):
    import app.bootstrap.workbench as bootstrap

    url = "https://models.example.test/v1"
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            row = await repo.add_credential(
                owner_actor_id=OWNER,
                sandbox_id=None,
                provider="one",
                ciphertext="never-read-secret",
                hint="hint",
                base_url=url,
            )
    credential = str(row["id"])
    base = {"provider": "one", "model": "kimi-k3", "family": "first"}
    alternate = {**base, "model": "second", "family": "second", "price_key": "kimi-k3"}
    tariff = {
        "provider": "one",
        "base_url": url,
        "price_key": "kimi-k3",
        "context_tokens": 1000,
        "output_tokens": 100,
    }
    settings = Settings(
        WORKBENCH_SANDBOX_MODEL_PROVIDER="one",
        WORKBENCH_SANDBOX_MODEL="kimi-k3",
        WORKBENCH_SANDBOX_PROVIDER_BASE_URL=url,
        WORKBENCH_REVIEW_ROUTES=[base, alternate],
        workbench_instruction_routes=[
            {**tariff, "model": r["model"]} for r in (base, alternate)
        ],
    )
    patch.setattr(bootstrap, "get_settings", lambda: settings)
    return reviewer_choices(), credential, url


async def test_reviewer_settings_are_owner_scoped_and_recheck_each_connection(
    db, monkeypatch
):
    policy, credential, url = await configured(db, monkeypatch)
    monkeypatch.setattr(routes, "reviewer_choices", lambda: policy)
    app = application(db, lambda repo: None)
    app.dependency_overrides[routes.require_workbench_enabled] = lambda: None

    async def session():
        async with db() as s:
            yield s

    app.dependency_overrides[routes.get_db_session] = session
    path = "/api/workbench/prefs/reviewer"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://testserver"
    ) as client:
        read = await client.get(path)
        assert read.status_code == 200 and "no-store" in read.headers["cache-control"]
        assert "never-read-secret" not in read.text
        (first,) = read.json()["options"]
        second = {**first["pin"], "model": "second"}
        assert first["destination"] == url + "/responses" and first["quote"]["maximum"]
        for invalid in (second, {**first["pin"], "credential_id": str(uuid4())}):
            assert (
                await client.put(path, json={"reviewer": invalid})
            ).status_code == 409
        policy.grants = (ReviewGrant(**second, base_url=url),)
        saved = await client.put(path, json={"reviewer": second})
        assert saved.status_code == 200 and saved.json()["preference"] == second
        app.dependency_overrides[get_web_current_user] = lambda: principal(OTHER)
        other = (await client.get(path)).json()
        assert other["options"] == [] and other["preference"] is None
        assert (await client.put(path, json={"reviewer": second})).status_code == 409
        app.dependency_overrides[get_web_current_user] = lambda: principal(OWNER)
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await repo.revoke_credential(credential, owner_actor_id=OWNER)
        stale = (await client.get(path)).json()
        assert stale["options"] == [] and stale["preference"] == second
        assert (await client.put(path, json={"reviewer": None})).json()[
            "preference"
        ] is None
