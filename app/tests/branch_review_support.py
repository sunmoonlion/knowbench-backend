"""A completed two-step trial and an owned, priced instruction for its received artifact."""

import json
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import partial
from uuid import uuid4

import httpx
from formal_plan_support import setup_plan
from formal_recursive_support import complete
from sqlalchemy import text
from test_formal_method_use_routes_db import application
from test_instruction_requests_db import PRICES, ROUTE
from test_workbench_ledger_db import OWNER

from app.application.workbench.branch_review_route import (
    RoutePreviewRequest,
    branch_route,
    review_projection,
    selected_quote,
)
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.instruction_requests import InstructionRequests
from app.application.workbench.reviewer_choices import ReviewerChoices
from app.application.workbench.source_tasks import hand_over_source
from app.domain.workbench.formal_execution import case_key
from app.domain.workbench.formal_method_execution_generated import (
    PrepareBranchReview,
    ReceiveBranch,
)
from app.domain.workbench.instruction_requests import leaf_responses_body
from app.domain.workbench.registry_sources import RegisteredSource, SourceDraft
from app.domain.workbench.review_routes import ReviewRoute
from app.domain.workbench.source_tasks import (
    REVIEW_KEY_PREFIX,
    ReviewBinding,
    SourceTask,
    instruction_input,
)
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import workbench_routes


async def seed_review_route(repo, run_id, case_id, step, revision, scope):
    await repo.session.execute(
        text(
            "INSERT INTO workbench_review_route_confirmations(run_id,case_id,step,revision,scope) "
            "VALUES (:run,:case,:step,:revision,cast(:scope as jsonb))"
        ),
        {
            "run": run_id,
            "case": case_id,
            "step": step,
            "revision": revision,
            "scope": json.dumps(scope),
        },
    )


async def existing_review(
    executions, policy, owner, run, selector, review_digest, source, sid
):
    """Reconstruct an in-flight pre-retirement task with its saved route and price."""
    repo = executions.repo
    request = RoutePreviewRequest(**selector.model_dump(), review_digest=review_digest)
    checked = await branch_route(executions, policy, owner, run, request)
    option = next(
        o for o in checked.page["options"] if o["digest"] == checked.page["recommended"]
    )
    binding = ReviewBinding(
        **request.model_dump(), run_id=run, revision=1, option_digest=option["digest"]
    )
    async with repo.transaction():
        await seed_review_route(
            repo,
            run,
            case_key(selector.case_id, selector.repetition),
            selector.step,
            1,
            {"option": option},
        )
        base = await (
            await executions.capabilities.instruction_service(
                executions.capabilities.formal_sources, owner
            )
        ).account_quote(owner, {"sandbox_id": checked.sandbox_id}, "CNY")
        snap = SourceTask(
            source=RegisteredSource.model_validate(
                {k: source[k] for k in RegisteredSource.model_fields}
            ),
            arguments={},
            budget_limit=Decimal(option["maximum"]),
            review_rounds=option["rounds"],
            quote=selected_quote(option, base),
            owner=owner,
            session_id=sid,
            review=binding,
        )
        return (
            await hand_over_source(repo, owner, snap, REVIEW_KEY_PREFIX + review_digest)
        )["task_id"]


async def review_case(db, monkeypatch, *, strict=False):
    import app.bootstrap.workbench as bootstrap

    ctx = await setup_plan(db, assessment=strict)
    case = (await complete(db, ctx))["cases"][0]
    report = case["verified_steps"]
    step = report["steps"][0]
    selector = ReceiveBranch(
        request_id=ctx.request.request_id,
        case_id=case["case_id"],
        repetition=case["repetition"],
        step=step["step"],
        scope_digest=report["scope_digest"],
    )
    run = str(ctx.request.run_id)
    policy = ReviewerChoices(
        (
            ReviewRoute(
                provider=ROUTE.provider, model=ROUTE.model, price_key=ROUTE.price_key
            ),
        ),
        (),
        (ROUTE,),
        PRICES,
        ROUTE.provider,
        ROUTE.model,
        ROUTE.base_url,
    )

    def service(repo):
        svc = ctx.factory(repo)
        svc.instruction_service = partial(
            InstructionRequests.for_account,
            routes=(ROUTE,),
            model=ROUTE.model,
            prices=PRICES,
            default_url=ROUTE.base_url,
        )

        async def projection(store, owner, binding, *, comparison_task=None):
            return await review_projection(
                FormalExecutions(store, ctx.factory(store)),
                policy,
                owner,
                binding,
                comparison_task=comparison_task,
            )

        svc.formal_sources.review_inputs = projection
        return svc

    monkeypatch.setattr(bootstrap, "capability_service", service)
    monkeypatch.setattr(workbench_routes, "reviewer_choices", lambda: policy)
    monkeypatch.setattr(bootstrap, "reviewer_choices", lambda: policy)
    text = "Check the approved output independently."
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = FormalExecutions(repo, ctx.factory(repo))
        await svc.receive_branch("author", run, selector, caller="knowlab")
        command = PrepareBranchReview.model_validate(
            {
                **selector.model_dump(),
                "artifact": step["artifact"],
                "previous": None,
                "criteria": {
                    **(
                        {
                            "comparison": {
                                "assessment_output": "self_assessment",
                                "max_reworks": 0,
                            }
                        }
                        if strict
                        else {}
                    ),
                    "outputs": ["receipt"],
                    "reviewer": "isolated_reviewer",
                    "assertions": [
                        {
                            "kind": "equals",
                            "pointer": "/receipt",
                            "value": "test@example.test",
                        }
                    ],
                },
            }
        )
        receipt = await svc.receive_branch("author", run, command, caller="knowlab")
        async with repo.transaction():
            sandbox = await repo.current_sandbox(OWNER)
            sessions = await repo.list_sessions(owner_actor_id=OWNER)
            chosen = next(
                s
                for s in sessions
                if not s.get("origin")
                and s["project_id"]
                and s["sandbox_id"] == sandbox["id"]
            )
            sid = str(chosen["id"])
            await repo.set_session_kind(sid, "expert")
            cred = await repo.add_credential(
                owner_actor_id=OWNER,
                provider=ROUTE.provider,
                ciphertext="fixture",
                hint="unit",
                sandbox_id=sandbox["id"],
                base_url=ROUTE.base_url,
            )
            source = await repo.register_direct_revision(
                OWNER,
                uuid4(),
                1,
                SourceDraft(kind="instruction", name="Review", content=text),
            )

    @asynccontextmanager
    async def host():
        async with db() as sql:
            svc = service(WorkbenchRepository(sql))
            yield await svc.instruction_service(svc.formal_sources, OWNER)

    app = application(db, service)
    app.dependency_overrides[workbench_routes.require_workbench_enabled] = lambda: None

    async def sessions_dep():
        async with db() as sql:
            yield sql

    app.dependency_overrides[workbench_routes.get_db_session] = sessions_dep
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://testserver"
    ) as client:
        url = f"/api/workbench/reviews/{run}/"
        scope = {
            **selector.model_dump(mode="json"),
            "review_digest": receipt["review"]["digest"],
        }
        for verb, path in (
            ("post", "route-preview"),
            ("put", "route"),
            ("post", "execution"),
        ):
            assert (
                await getattr(client, verb)(url + path, json=scope)
            ).status_code == 404
    async with db() as sql:
        repo = WorkbenchRepository(sql)
        task = await existing_review(
            FormalExecutions(repo, service(repo)),
            policy,
            OWNER,
            run,
            selector,
            receipt["review"]["digest"],
            source,
            sid,
        )
    job = instruction_input(text, {"receipt": "test@example.test"})
    wire = leaf_responses_body(job, ROUTE.model, ROUTE.output_tokens)
    wire["input"] = wire["input"][-1:]
    return host, task, {"id": str(cred["id"])}, wire
