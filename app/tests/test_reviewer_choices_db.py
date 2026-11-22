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


async def test_confirm_route_on_received_artifact_replays_and_rechecks_v0(
    db, monkeypatch
):
    import asyncio

    from formal_plan_support import setup_plan
    from formal_recursive_support import complete

    from app.application.workbench.formal_test_runs import FormalExecutions
    from app.domain.workbench.formal_method_execution_generated import (
        PrepareBranchReview,
        ReceiveBranch,
    )

    ctx = await setup_plan(db)
    result = await complete(db, ctx)
    case = result["cases"][0]
    report = case["verified_steps"]
    step = report["steps"][0]
    scope = dict(
        request_id=str(ctx.request.request_id),
        case_id=case["case_id"],
        repetition=case["repetition"],
        step=step["step"],
        scope_digest=report["scope_digest"],
    )
    run_id = str(ctx.request.run_id)
    policy, credential, _ = await configured(db, monkeypatch)
    monkeypatch.setattr(routes, "reviewer_choices", lambda: policy)
    app = application(db, lambda repo: None)
    app.dependency_overrides[routes.require_workbench_enabled] = lambda: None

    async def sessions():
        async with db() as session:
            yield session

    app.dependency_overrides[routes.get_db_session] = sessions

    async def prepare_review(expected, previous):
        async with db() as session:
            repo = WorkbenchRepository(session)
            svc = FormalExecutions(repo, ctx.factory(repo))
            criteria = {
                "outputs": ["receipt"],
                "reviewer": "isolated_reviewer",
                "assertions": [
                    {"kind": "equals", "pointer": "/receipt", "value": expected}
                ],
            }
            command = PrepareBranchReview.model_validate(
                {
                    **scope,
                    "artifact": step["artifact"],
                    "criteria": criteria,
                    "previous": previous,
                }
            )
            return (
                await svc.receive_branch("author", run_id, command, caller="knowlab")
            )["review"]["digest"]

    async with db() as session:
        repo = WorkbenchRepository(session)
        await FormalExecutions(repo, ctx.factory(repo)).receive_branch(
            "author", run_id, ReceiveBranch(**scope), caller="knowlab"
        )
        async with repo.transaction():
            await repo.purge_test_run(run_id)
    review = await prepare_review("test@example.test", None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://testserver"
    ) as client:
        url = f"/api/workbench/reviews/{run_id}/route"
        payload = {**scope, "review_digest": review}
        preview = await client.post(url + "-preview", json=payload)
        assert preview.status_code == 200, preview.text
        page = preview.json()
        assert (
            set(page["handoff"]) == {"receipt"}
            and page["isolated_execution_available"] is False
        )
        assert "test@example.test" not in preview.text
        choice = page["options"][0]
        assert (
            choice["pin"]["credential_id"] == credential
            and page["recommended"] == choice["digest"]
        )
        command = {**payload, "option_digest": choice["digest"], "previous_revision": 0}
        responses = await asyncio.gather(
            *(client.put(url, json=command) for _ in range(2))
        )
        assert all(
            r.status_code == 200 and r.json()["revision"] == 1 for r in responses
        )
        assert (await client.post(url + "-preview", json=payload)).json()[
            "confirmed"
        ] == choice["digest"]
        original_routes = policy.routes
        policy.routes = (
            original_routes[0].model_copy(update={"family": "reclassified"}),
            *original_routes[1:],
        )
        changed = (await client.post(url + "-preview", json=payload)).json()
        assert changed["confirmed"] is None and changed["recommended"] is None
        assert changed["options"][0]["planned_producer"]["family"] == "reclassified"
        assert (await client.put(url, json=command)).status_code == 409
        policy.routes = original_routes
        original_tariffs = policy.tariffs
        policy.tariffs = ()
        unpriced = (await client.post(url + "-preview", json=payload)).json()
        assert unpriced["gap"] == "quote_unavailable" and not unpriced["options"]
        policy.tariffs = original_tariffs
        app.dependency_overrides[get_web_current_user] = lambda: principal(OTHER)
        assert (await client.put(url, json=command)).status_code == 404
        app.dependency_overrides[get_web_current_user] = lambda: principal(OWNER)
        async with db() as session:
            repo = WorkbenchRepository(session)
            async with repo.transaction():
                await repo.revoke_credential(credential, owner_actor_id=OWNER)
        assert (await client.put(url, json=command)).status_code == 409
        assert (await client.post(url + "-preview", json=payload)).json()[
            "confirmed"
        ] is None
        failed = await prepare_review("wrong", review)
        assert (await client.put(url, json=command)).status_code == 409
        blocked = (
            await client.post(
                url + "-preview", json={**payload, "review_digest": failed}
            )
        ).json()
        assert (
            blocked["gap"] == "v0_failed"
            and not blocked["handoff"]
            and not blocked["options"]
        )
    assert len(ctx.remote.calls) == 2
