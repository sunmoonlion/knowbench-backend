"""Run the frozen comparison from an actual author trial through an ordinary delivery."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from functools import partial
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from formal_plan_support import freeze, plan_request
from sqlalchemy import text
from test_capabilities_db import Remote, approval
from test_formal_execution_db import approve, settle, status
from test_formal_instruction_trial_db import AuthorTrial
from test_instruction_requests_db import PRICES, ROUTE
from test_instruction_runner_db import drive as drive_trial
from test_instruction_runner_db import stack
from test_ordinary_expert_execution_db import selected_match
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db
from test_workbench_routes import make_client as make_client
from test_workbench_task_runtime_db import drive

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.formal_execution import FormalExecution
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.instruction_requests import InstructionRequests
from app.application.workbench.reviewer_choices import ReviewerChoices
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.review_routes import ReviewGrant, ReviewRoute
from app.infrastructure.workbench.repository import WorkbenchRepository


async def setup_review(db, *, max_reworks=0, dual=False):
    trial = AuthorTrial()
    s = await stack(db, author_trial=trial.start, registered=True, review_fixture=True)
    s.units.ready = True
    await drive_trial(s)
    leaf = await trial.finish(db)
    cipher = Fernet(Fernet.generate_key())
    alternate = replace(
        ROUTE,
        model="review-model",
        provider="alternate",
        base_url="https://review.example.test/v1",
    )
    async with db() as session, session.begin():
        repo = WorkbenchRepository(session)
        await session.execute(
            text("UPDATE workbench_credentials SET ciphertext=:key WHERE id=:id"),
            {"id": s.cred["id"], "key": cipher.encrypt(b"fixture-primary").decode()},
        )
        cred = await repo.add_credential(
            owner_actor_id=OWNER,
            provider=alternate.provider,
            base_url=alternate.base_url,
            ciphertext=cipher.encrypt(b"fixture-b").decode(),
            sandbox_id=s.cred["sandbox_id"],
            hint="alt",
        )
        pin = {
            "provider": alternate.provider,
            "model": alternate.model,
            "credential_id": str(cred["id"]),
        }
        await repo.save_reviewer_preference(OWNER, pin)
    replacement = replace(alternate, model="review-replacement")
    policy = ReviewerChoices(
        tuple(
            ReviewRoute(
                provider=r.provider, model=r.model, price_key=r.price_key, family=f
            )
            for r, f in (
                (ROUTE, "primary"),
                (alternate, "review"),
                (replacement, "review"),
            )
        ),
        tuple(
            ReviewGrant(**{**pin, "model": r.model}, base_url=r.base_url)
            for r in (alternate, replacement)
        ),
        (ROUTE, alternate, replacement),
        PRICES,
        ROUTE.provider,
        ROUTE.model,
        ROUTE.base_url,
    )
    route = partial(
        InstructionRequests.for_account,
        routes=(ROUTE, alternate, replacement),
        model=ROUTE.model,
        prices=PRICES,
        default_url=ROUTE.base_url,
    )

    def factory(repo):
        caps = Capabilities(repo, Remote(), instruction_service=route)
        caps.formal_sources = trial.factory(repo).formal_sources
        caps.formal_sources.review_policy = policy
        return caps

    s.driver.formal = lambda repo: factory(repo).formal_sources
    s.driver.credential_cipher = cipher
    s.units.review_fixture = True
    conductor = KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory)
    driver = FormalExecution(workbench_stores(db), factory, instructions=s.driver)
    case = trial.request.execution.suite.cases[0].model_dump(mode="json")
    case["input"]["values"] = {"status": "approved source"}
    if dual:
        case["input"]["values"]["other"] = "separate source"
    req = plan_request(
        trial.request, leaf, case, input_name="status", output_name="summary"
    )
    doc = req.model_dump(mode="json", by_alias=True)
    graph = doc["draft"]["definition"]["procedure"]
    review = next(step for step in graph["steps"] if step["id"] == "second")
    review["inputs"]["assessment"]["binding"] = {
        "source": "step",
        "step": "first",
        "output": "assessment",
    }
    graph.update(
        max_steps=12,
        max_expansions=13,
        collaboration={
            "first": {"isolation_group": "author"},
            "second": {
                "isolation_group": "reviewer",
                "review_requirements": {"first": "different_model_family"},
                "handoff_from": ["first"],
                "comparison": {
                    "assessment_output": "assessment",
                    "max_reworks": max_reworks,
                },
            },
        },
    )
    if dual:
        first = next(x for x in graph["steps"] if x["id"] == "first")
        other = {**deepcopy(first), "id": "independent"}
        other["inputs"]["status"]["binding"]["name"] = "other"
        graph["steps"].append(other)
        roles = graph["collaboration"]
        roles["independent"] = {"isolation_group": "another"}
        roles["second"].pop("comparison")
        roles["second"]["review_requirements"]["independent"] = "different_model_family"
        roles["second"]["handoff_from"].append("independent")
        review["depends_on"].append("independent")
        review["inputs"]["assessment"]["binding"].update(
            step="independent", output="summary"
        )
    req = type(req).model_validate(doc)
    async with s.host() as svc:
        await FormalExecutions(svc.repo, factory(svc.repo)).accept(
            req, caller="knowlab"
        )
    await conductor.advance(str(req.run_id))
    return s, factory, leaf, req, driver, conductor


async def wire(
    db,
    s,
    factory,
    task,
    attempt,
    caller,
    phase,
    *,
    bad_receipt=False,
    needs_rework=False,
):
    s.units.task = task
    s.units.uid, s.units.worker, s.units.thread = map(str, (uuid4(), uuid4(), uuid4()))
    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = await factory(repo).instruction_service(
            factory(repo).formal_sources, OWNER
        )
        grants = (
            await svc.formal.registry.task_grants(task, owner=OWNER)
            if caller == "formal-expert"
            else None
        )
        quote, job = await svc.authority(
            OWNER, await repo.get_task(task), caller, grants=grants
        )
    s.wire["input"][0]["content"][0]["text"] = json.dumps(job["input"])
    s.wire["text"]["format"]["schema"] = job["output_schema"]
    s.wire["model"] = quote["model"]
    s.units.reported_model = "unapproved-model" if bad_receipt else quote["model"]
    args = job["input"]["arguments"]
    if phase in ("producer", "revision"):
        if phase == "revision":
            assert (
                args["prior_summary"] == "checked work"
                and args["feedback_summary"] == "ready"
                and args["feedback_needs_rework"] is True
            )
        s.units.result = {"summary": "checked work", "assessment": "author-only"}
    elif phase == "independent":
        assert args == {"status": "separate source"}
        s.units.result = {
            "summary": "independent findings",
            "assessment": "second private assessment",
        }
    elif phase == "join":
        assert args == {"status": "checked work", "assessment": "independent findings"}
        assert "author-only" not in json.dumps(
            job
        ) and "second private assessment" not in json.dumps(job)
        s.units.result = {"summary": "ready"}
    elif phase == "blind":
        assert "author-only" not in json.dumps(job) and set(args) == {"status"}
        s.units.result = {"summary": "sealed findings"}
    else:
        assert (
            args["assessment"] == "author-only"
            and args["finding_summary"] == "sealed findings"
        )
        s.units.result = {"summary": "ready", "needs_rework": needs_rework}
    await s.driver.drive(task, leaf_attempt_id=attempt)
    assert s.provider_requests[-1]["model"] == quote["model"]


@pytest.mark.parametrize(
    "scenario",
    [
        "pass",
        "wrong_model",
        "changed_route",
        "two_reworks",
        "stop",
        "budget",
        "no_route",
        "bad_blind",
        "two_sources",
        "received",
    ],
)
async def test_author_strict_then_ordinary_cross_family(
    db, monkeypatch, make_client, scenario
):
    dual = scenario == "two_sources"
    initial_phases = (
        ["producer", "independent", "join"]
        if dual
        else ["producer", "blind", "comparison"]
    )
    bad_receipt = scenario == "wrong_model"
    rework = scenario in ("two_reworks", "changed_route", "stop", "budget", "no_route")
    s, factory, leaf, req, driver, conductor = await setup_review(
        db, max_reworks=2 if rework else 0, dual=dual
    )
    for phase in initial_phases:
        run = await status(db, req, factory)
        task = run["cases"][0]["task_id"]
        assert len(run["actions"]) == 1, run
        await approve(db, req, factory, run)
        async with db() as session:
            attempt = (await WorkbenchRepository(session).get_task(task))[
                "active_attempt_id"
            ]
        await wire(db, s, factory, task, str(attempt), "knowlab", phase)
        await driver.drive(task)
        await conductor.advance(str(req.run_id))
    await settle(conductor, req)
    run = await status(db, req, factory)
    assert run["status"] == "passed", run
    if scenario == "received":
        await check_received_model(db, s, factory, req, run)
        return
    expert = freeze(req, run)
    async with db() as session:
        await factory(WorkbenchRepository(session)).formal_sources.register(
            expert, caller="knowlab"
        )
    qualified = {expert.reference, leaf.reference}
    async with selected_match(
        db, monkeypatch, expert, qualified, OWNER, factory
    ) as match:
        client, path, selected, runner, scoped, _ = match
        runner.instructions = s.driver
        s.driver.formal = lambda repo: scoped(repo).formal_sources
        prep = await client.put(
            path + "/preparation",
            json={
                "expected_revision": 0,
                "review_digest": selected["review_digest"],
                "budget_limit": "0.15" if scenario == "budget" else "2",
                "values": {
                    "status": "approved source",
                    **({"other": "separate source"} if dual else {}),
                },
                "accounts": {},
                "materials": {},
            },
        )
        assert prep.status_code == 200, prep.text
        preview = (await client.get(path + "/execution-preview?revision=1")).json()
        assert not preview["issues"], preview
        assert [step["quote"]["model"] for step in preview["steps"]] == [
            ROUTE.model,
            ROUTE.model if dual else "review-model",
            "review-model",
        ]
        confirmed = await client.put(
            path + "/execution",
            json={"revision": 1, "preview_digest": preview["preview_digest"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        task = confirmed.json()["task_id"]
        initial = len(s.provider_requests)
        phases = initial_phases.copy()
        if rework and scenario not in ("budget", "no_route"):
            phases += ["revision", "blind", "comparison"] * (
                1 if scenario == "changed_route" else 2
            )

        async def adjudicate(kind, choice):
            async with db() as session:
                repo = WorkbenchRepository(session)
                row = await repo.get_task(task)
                assert row["state"] == "WAITING", row["state"]
                iid = str(row["active_interaction_id"])
                it = await repo.get_interaction(iid)
                assert it["kind"] == kind, it["prompt"]
                events = await repo.list_task_events(
                    task, types=("interaction/opened",)
                )
                token = next(
                    e["payload"]["token"]
                    for e in events
                    if e["payload"]["interaction_id"] == iid
                )
            view = await client.get(f"/api/workbench/interactions/{iid}")
            assert view.status_code == 200, view.text
            assert (
                view.json()["interaction"]["subject"]["digest"] == it["subject_digest"]
            )
            url = f"/api/workbench/interactions/{iid}/respond"
            payload = {
                "decision": choice,
                "token": token,
                "subject_digest": it["subject_digest"],
            }
            assert (
                await client.post(url, json={**payload, "subject_digest": "0" * 64})
            ).status_code == 409
            response = await client.post(url, json=payload)
            assert response.status_code == 200, response.text
            assert (await client.post(url, json=payload)).status_code == 409

        for index, phase in enumerate(phases):
            await drive(runner)
            async with db() as session:
                repo = WorkbenchRepository(session)
                attempt = (await repo.list_attempts(task))[-1]
                call = await repo.capability_call(
                    OWNER, attempt["refs"]["capability_call_id"]
                )
                svc = scoped(repo)
                from app.application.workbench.artifact_read_authority import (
                    ArtifactConsent,
                )

                body = approval(call)
                read = call["detail"].get("artifact_read")
                if read:
                    from datetime import UTC, datetime, timedelta

                    body = body.model_copy(
                        update={
                            "artifact_consent": ArtifactConsent(
                                scope_digest=read["scope_digest"],
                                expires_at=datetime.now(UTC) + timedelta(hours=1),
                            )
                        }
                    )
                await svc.decide(OWNER, call["id"], body)
            await wire(
                db,
                s,
                scoped,
                task,
                str(attempt["id"]),
                "formal-expert",
                phase,
                bad_receipt=(bad_receipt and phase == "producer")
                or (scenario == "bad_blind" and phase == "blind"),
                needs_rework=rework and (scenario != "changed_route" or index == 2),
            )
            if scenario == "changed_route" and index == 2:
                async with db() as session, session.begin():
                    repo = WorkbenchRepository(session)
                    old = await repo.reviewer_preference(OWNER)
                    await repo.save_reviewer_preference(
                        OWNER, {**old, "model": "review-replacement"}
                    )
                await drive(runner)
                assert len(s.provider_requests) == initial + 3
                await adjudicate("review_route_confirmation", "confirm_route")
            if scenario == "no_route" and index == 2:
                async with db() as session, session.begin():
                    repo = WorkbenchRepository(session)
                    old = await repo.reviewer_preference(OWNER)
                    await repo.save_reviewer_preference(
                        OWNER, {**old, "model": "unavailable"}
                    )
            if bad_receipt or (scenario == "bad_blind" and phase == "blind"):
                await drive(runner)
                async with db() as session:
                    saved = await WorkbenchRepository(session).get_task(task)
                    assert (
                        saved["state"] == "FAILED"
                        and len(s.provider_requests) == initial + index + 1
                    )
                return
        await drive(runner)
        if scenario in ("budget", "no_route"):
            assert len(s.provider_requests) == initial + 3
            await adjudicate("review_adjudication", "accept_delivery")
        if scenario in ("two_reworks", "stop"):
            await drive(runner)
            assert len(s.provider_requests) == initial + 9
            await adjudicate(
                "review_adjudication",
                "stop" if scenario == "stop" else "accept_delivery",
            )
        delivery = (await client.get(path)).json()
        if scenario == "stop":
            assert delivery["execution_state"] == "CANCELLED"
            return
        if rework and scenario not in ("budget", "no_route"):
            from app.application.workbench.delegation_receipts import handoffs

            async with db() as session:
                repo = WorkbenchRepository(session)
                handback = (await handoffs(repo, OWNER, task))["parents"][0]
                assert handback["missing"] == []
                reviewer = next(
                    c
                    for c in handback["children"]
                    if c["rounds"][0]["request"]["role"] == ["second"]
                )
                finals = [r["receipts"][-1] for r in reviewer["rounds"]]
                assert all(
                    r["state"] == "COMPLETED" and Decimal(r["cost"]) > 0 for r in finals
                )
                models = [r["executor"]["reported_model"] for r in finals]
                assert models == (
                    ["review-model"] * 2 + ["review-replacement"] * 2
                    if scenario == "changed_route"
                    else ["review-model"] * 6
                )
                task_row = await repo.get_task(task)
                assert Decimal(task_row["budget"]["used"]) == sum(
                    Decimal(r["receipts"][-1]["cost"])
                    for c in handback["children"]
                    for r in c["rounds"]
                )

        assert delivery["execution_state"] == "SUCCEEDED" and list(
            delivery["delivery"].values()
        ) == ["ready"]
        if scenario == "two_sources":
            from expert_duty_support import verify_period

            await verify_period(
                db, monkeypatch, client, path, delivery, expert.reference, qualified
            )
        assert len(s.provider_requests) == initial + len(phases)


async def check_received_model(db, s, factory, req, run):
    from app.application.workbench.branch_review_dispatch import (
        DispatchReview,
        dispatch_review,
        review_projection,
    )
    from app.application.workbench.branch_review_route import (
        RouteConfirmation,
        RoutePreviewRequest,
        branch_route,
    )
    from app.application.workbench.ledger import Ledger
    from app.application.workbench.session_service import SessionService
    from app.domain.workbench.formal_method_execution_generated import (
        PrepareBranchReview,
        ReceiveBranch,
    )
    from app.domain.workbench.registry_sources import SourceDraft
    from app.domain.workbench.states import TaskState

    case = run["cases"][0]
    report = case["verified_steps"]
    step = next(row for row in report["steps"] if row["step"] == "first")
    selector = ReceiveBranch(
        request_id=req.request_id,
        case_id=case["case_id"],
        repetition=case["repetition"],
        step="first",
        scope_digest=report["scope_digest"],
    )
    rid = str(req.run_id)

    def formal(repo):
        methods = factory(repo).formal_sources

        async def inputs(store, owner, binding, *, comparison_task=None):
            return await review_projection(
                FormalExecutions(store, factory(store)),
                methods.review_policy,
                owner,
                binding,
                comparison_task=comparison_task,
            )

        methods.review_inputs = inputs
        return methods

    s.driver.formal = formal
    async with db() as session:
        repo = WorkbenchRepository(session)
        executions = FormalExecutions(repo, factory(repo))
        subject = await repo.subject_of_actor(OWNER)
        policy = formal(repo).review_policy
        await executions.receive_branch(subject, rid, selector, caller="knowlab")
        receipt = await executions.receive_branch(
            subject,
            rid,
            PrepareBranchReview.model_validate(
                {
                    **selector.model_dump(),
                    "artifact": step["artifact"],
                    "previous": None,
                    "criteria": {
                        "outputs": ["summary"],
                        "reviewer": "different_model_family",
                        "assertions": [
                            {
                                "kind": "equals",
                                "pointer": "/summary",
                                "value": "checked work",
                            }
                        ],
                    },
                }
            ),
            caller="knowlab",
        )
        request = RoutePreviewRequest(
            **selector.model_dump(), review_digest=receipt["review"]["digest"]
        )
        view = (await branch_route(executions, policy, OWNER, rid, request)).page
        assert view["verified_producer"]["model"] == ROUTE.model
        from unittest.mock import AsyncMock, patch

        from app.application.workbench.branch_review_dispatch import dispatch_choices

        with patch.object(repo, "sealed_artifact_model", AsyncMock(return_value=None)):
            legacy = (await branch_route(executions, policy, OWNER, rid, request)).page
            saved = await branch_route(
                executions,
                policy,
                OWNER,
                rid,
                RouteConfirmation(
                    **request.model_dump(),
                    previous_revision=0,
                    option_digest=legacy["recommended"],
                ),
            )
            assert not saved.page.get("verified_producer")
            assert await dispatch_choices(executions, OWNER, saved) is None
        assert not (await branch_route(executions, policy, OWNER, rid, request)).page[
            "confirmed"
        ]
        confirmation = RouteConfirmation(
            **request.model_dump(),
            previous_revision=1,
            option_digest=view["recommended"],
        )
        await branch_route(executions, policy, OWNER, rid, confirmation)
        async with repo.transaction():
            sessions = await repo.list_sessions(owner_actor_id=OWNER)
            env = next(x["environment_id"] for x in sessions if x.get("environment_id"))
            project = await repo.create_project(
                owner_actor_id=OWNER,
                environment_id=str(env),
                workspace_root="/home/u/research",
                path="review",
                title="Received review",
            )
            source = await repo.register_direct_revision(
                OWNER,
                uuid4(),
                1,
                SourceDraft(
                    kind="instruction",
                    name="Review",
                    content="Check only the received summary.",
                ),
            )
        sid = (
            await SessionService(repo).start(
                owner_actor_id=OWNER, project_id=project, kind="expert"
            )
        )["session_id"]
        body = DispatchReview(
            **request.model_dump(),
            revision=2,
            option_digest=view["recommended"],
            session_id=sid,
            source_id=source["source_id"],
            version=1,
            digest=source["digest"],
        )
        task = (await dispatch_review(executions, policy, OWNER, rid, body))["task_id"]
        ledger = Ledger(repo)
        await ledger.transition(task, TaskState.VALIDATING)
        await ledger.transition(task, TaskState.QUEUED)
        svc = await factory(repo).instruction_service(formal(repo), OWNER)
        aid = await svc.open(OWNER, task, caller="registry-source", managed=True)
        quote, job = await svc.authority(
            OWNER, await repo.get_task(task), "registry-source"
        )
        assert job["input"]["arguments"] == {"summary": "checked work"}
    s.units.task = task
    s.units.uid, s.units.worker, s.units.thread = map(str, (uuid4(), uuid4(), uuid4()))
    s.units.result = {"result": "Independently accepted"}
    s.units.reported_model = quote["model"]
    assert s.units.reported_model == "review-model"
    s.wire["model"] = quote["model"]
    s.wire["input"][0]["content"][0]["text"] = json.dumps(job["input"])
    s.wire["text"]["format"]["schema"] = job["output_schema"]
    await s.driver.drive(task)
    async with db() as session:
        repo = WorkbenchRepository(session)
        assert (await repo.get_task(task))["state"] == "SUCCEEDED"
        attempt = await repo.get_attempt(aid)
        assert attempt["refs"]["instruction_usage"]["model"] == "review-model"
