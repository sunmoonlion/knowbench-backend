"""A completed two-step trial and an owned, priced instruction for its received artifact."""

import asyncio
from contextlib import asynccontextmanager
from functools import partial
from uuid import uuid4

import httpx
from formal_plan_support import setup_plan
from formal_recursive_support import complete
from test_formal_method_use_routes_db import application
from test_instruction_requests_db import PRICES, ROUTE
from test_workbench_ledger_db import OWNER

from app.application.workbench.branch_review_dispatch import review_projection
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.instruction_requests import InstructionRequests
from app.application.workbench.reviewer_choices import ReviewerChoices
from app.domain.workbench.formal_method_execution_generated import (
    PrepareBranchReview,
    ReceiveBranch,
)
from app.domain.workbench.instruction_requests import leaf_responses_body
from app.domain.workbench.registry_sources import SourceDraft
from app.domain.workbench.review_routes import ReviewRoute
from app.domain.workbench.source_tasks import instruction_input
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import workbench_routes


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
        page = (await client.post(url + "route-preview", json=scope)).json()
        option = page["recommended"]
        payload = {**scope, "option_digest": option}
        body = {
            **payload,
            "revision": 1,
            "source_id": str(source["source_id"]),
            "version": 1,
            "digest": source["digest"],
            "session_id": sid,
        }
        assert (await client.post(url + "execution", json=body)).status_code == 409
        confirmation = await client.put(
            url + "route", json={**payload, "previous_revision": 0}
        )
        assert confirmation.json()["isolated_execution_available"], confirmation.text
        assert (
            await client.post(url + "execution", json={**body, "revision": 2})
        ).status_code == 409
        responses = await asyncio.gather(
            *(client.post(url + "execution", json=body) for _ in range(2))
        )
        for r in responses:
            assert r.status_code == 200, r.text
        assert responses[0].json() == responses[1].json()
        task = responses[0].json()["task_id"]
        resumed = (await client.post(url + "route-preview", json=scope)).json()[
            "execution"
        ]
        assert resumed["task_id"] == task and resumed["session_id"] == sid
    job = instruction_input(text, {"receipt": "test@example.test"})
    wire = leaf_responses_body(job, ROUTE.model, ROUTE.output_tokens)
    wire["input"] = wire["input"][-1:]
    return host, task, {"id": str(cred["id"])}, wire
