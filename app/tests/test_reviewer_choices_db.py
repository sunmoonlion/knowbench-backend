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


async def test_saved_route_rechecks_identity_price_credential_and_v0(db, monkeypatch):
    from branch_review_support import seed_review_route
    from formal_plan_support import setup_plan
    from formal_recursive_support import complete

    from app.application.workbench.branch_review_route import (
        RoutePreviewRequest,
        branch_route,
    )
    from app.application.workbench.formal_test_runs import FormalExecutions
    from app.domain.workbench.formal_execution import case_key
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

    async def read_saved(digest):
        async with db() as session:
            repo = WorkbenchRepository(session)
            checked = await branch_route(
                FormalExecutions(repo, ctx.factory(repo)),
                policy,
                OWNER,
                run_id,
                RoutePreviewRequest(**scope, review_digest=digest),
            )
            return checked.page

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
    page = await read_saved(review)
    assert set(page["handoff"]) == {"receipt"} and "test@example.test" not in str(page)
    choice = page["options"][0]
    assert choice["pin"]["credential_id"] == credential
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await seed_review_route(
                repo,
                run_id,
                case_key(case["case_id"], case["repetition"]),
                step["step"],
                1,
                {"option": choice},
            )
    assert (await read_saved(review))["confirmed"] == choice["digest"]
    original_routes = policy.routes
    policy.routes = (
        original_routes[0].model_copy(update={"family": "reclassified"}),
        *original_routes[1:],
    )
    changed = await read_saved(review)
    assert changed["confirmed"] is None
    assert changed["options"][0]["planned_producer"]["family"] == "reclassified"
    policy.routes = original_routes
    tariffs = policy.tariffs
    policy.tariffs = ()
    unpriced = await read_saved(review)
    assert unpriced["gap"] == "quote_unavailable" and not unpriced["options"]
    policy.tariffs = tariffs
    async with db() as session:
        repo = WorkbenchRepository(session)
        async with repo.transaction():
            await repo.revoke_credential(credential, owner_actor_id=OWNER)
    assert (await read_saved(review))["confirmed"] is None
    failed = await prepare_review("wrong", review)
    blocked = await read_saved(failed)
    assert (
        blocked["gap"] == "v0_failed"
        and not blocked["handoff"]
        and not blocked["options"]
    )
    assert len(ctx.remote.calls) == 2


async def test_replacing_v0_after_reservation_blocks_first_paid_review_request(
    db, monkeypatch
):
    import pytest
    from branch_review_support import review_case

    from app.application.workbench.formal_test_runs import FormalExecutions
    from app.application.workbench.ledger import Ledger
    from app.bootstrap.workbench import capability_service
    from app.domain.workbench.capabilities import CapabilityError
    from app.domain.workbench.formal_method_execution_generated import (
        PrepareBranchReview,
        ReceiveBranch,
    )
    from app.domain.workbench.source_tasks import SourceTask

    host, task_id, cred, wire = await review_case(db, monkeypatch)
    async with host() as svc:
        repo = svc.repo
        ledger = Ledger(repo)
        await ledger.transition(task_id, "VALIDATING")
        await ledger.transition(task_id, "QUEUED")
        aid = await svc.open(OWNER, task_id, caller="registry-source", managed=True)
        snap = SourceTask.load(await repo.get_task(task_id))
        binding = snap.review
        selector = ReceiveBranch.model_validate(
            binding.model_dump(include=set(ReceiveBranch.model_fields))
        )
        executions = FormalExecutions(repo, capability_service(repo))
        run = str(binding.run_id)
        delivery = await executions.receive_branch(
            "author", run, selector, caller="knowlab", received_only=True
        )
        criteria = delivery["review"]["criteria"]
        criteria["assertions"][0]["value"] = "superseded assumption"
        replacement = PrepareBranchReview.model_validate(
            {
                **selector.model_dump(),
                "artifact": delivery["artifact"],
                "previous": binding.review_digest,
                "criteria": criteria,
            }
        )
        assert (
            await executions.receive_branch(
                "author", run, replacement, caller="knowlab"
            )
        )["review"]["status"] == "v0_failed"
        with pytest.raises(CapabilityError, match="confirmation is no longer active"):
            await svc.claim(
                OWNER,
                task_id,
                aid,
                caller="registry-source",
                credential_id=cred["id"],
                turn_id="review-turn",
                body=wire,
            )
        attempt = await repo.get_attempt(aid)
        assert (
            "instruction_request" not in attempt["refs"]
            and "instruction_usage" not in attempt["refs"]
        )
