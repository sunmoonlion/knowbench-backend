"""A qualified two-step expert, normal buyer confirmation, real ledger and synthetic MCP."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
import pytest
from formal_plan_support import setup_plan
from formal_recursive_support import register_completed, setup_recursive
from sqlalchemy import text
from test_capabilities_db import approval
from test_formal_expert_intake_db import web_app
from test_knowbot_test_runs_db import runner_for
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_ledger_db import db as db

from app.application.workbench.find_expert import FindExpertService
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.formal_recursive import node_key
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints import capability_routes
from app.interfaces.http.web import workbench_expert as routes


@pytest.mark.parametrize(
    ("nested", "revoke"),
    [
        (False, None),
        (True, None),
        (False, "qualification"),
        (True, "qualification"),
        (True, "parent_scope"),
    ],
)
async def test_normal_execution_uses_confirmed_revision_and_per_step_approval(
    db, monkeypatch, revoke, nested
):
    ctx = await setup_recursive(db) if nested else await setup_plan(db)
    expert = await register_completed(db, ctx)
    ctx.remote.calls.clear()
    qualified = {expert.reference, ctx.leaf.reference}
    if nested:
        qualified.update(e.reference for e in ctx.experts)
    steps = 4 if nested else 2

    async with selected_match(
        db, monkeypatch, expert, qualified, ctx.owner, ctx.factory
    ) as match:
        client, path, selected, runner, factory, model = match
        try:
            prep = (await client.get(path + "/preparation")).json()
            bindings = {
                "expected_revision": 0,
                "review_digest": selected["review_digest"],
                "budget_limit": "1",
                "values": {"to": "test@example.test"},
                "accounts": {
                    k: v[0]["pin"] for k, v in prep["account_options"].items()
                },
                "materials": {},
            }
            assert (
                await client.put(path + "/preparation", json=bindings)
            ).status_code == 200
            view = (await client.get(path + "/execution-preview?revision=1")).json()
            assert not view["dispatch_gap"] and not view["issues"], view
            assert view["steps"][1]["from_outputs"]["to"] == {
                "step": ["first", "first"] if nested else ["first"],
                "output": "receipt",
            }
            if nested:
                assert view["scope_hours"] == 24
                assert [e["path"] for e in view["subexperts"]] == [
                    ["first"],
                    ["second"],
                ]
                assert all(
                    e["reference"] == ctx.experts[0].reference.model_dump(mode="json")
                    for e in view["subexperts"]
                )
                assert view["steps"][2]["from_outputs"]["to"] == {
                    "step": ["first"],
                    "output": "final_receipt",
                }
            body = {"revision": 1, "preview_digest": view["preview_digest"]}
            refused = await client.put(
                path + "/execution",
                json={**body, "preview_digest": "sha256:" + "0" * 64},
            )
            assert refused.status_code == 409
            responses = await asyncio.gather(
                *(client.put(path + "/execution", json=body) for _ in range(2))
            )
            assert responses[0].status_code == 200, responses[0].text
            receipt = responses[0].json()
            assert receipt == responses[1].json()
            task = receipt["task_id"]
            assert (await client.get(path)).json()["execution"] == receipt
            for i in range(steps):
                await drive(runner)
                async with db() as s:
                    repo = WorkbenchRepository(s)
                    attempts = await repo.list_attempts(task)
                    assert len(attempts) == i + 1, attempts
                    call = await repo.capability_call(
                        ctx.owner, attempts[-1]["refs"]["capability_call_id"]
                    )
                    assert (
                        call["status"] == "awaiting_approval"
                        and len(ctx.remote.calls) == i
                    )
                    assert await repo.test_run_for_task(task) is None
                if i == 1 and not revoke:
                    await runner._shutdown()
                    runner = runner_for(db)
                    runner.capabilities = factory
                    await drive(runner)
                    async with db() as s:
                        restored = await WorkbenchRepository(s).capability_call(
                            ctx.owner, call["id"]
                        )
                        assert restored["status"] == "awaiting_approval"
                        assert restored["interaction_id"] == call["interaction_id"]
                        assert len(ctx.remote.calls) == 1
                if revoke and i == 1:
                    if revoke == "parent_scope":
                        async with db() as s:
                            repo = WorkbenchRepository(s)
                            async with repo.transaction():
                                parent = await repo.call_by_key(
                                    ctx.owner,
                                    call["session_id"],
                                    node_key(task, ["first"]),
                                )
                                auth = await repo.method_authorization(
                                    ctx.owner, parent["id"], lock=True
                                )
                                await repo.save_method_authorization(
                                    ctx.owner,
                                    call["session_id"],
                                    {
                                        **auth,
                                        "revoked": True,
                                        "revision": auth["revision"] + 1,
                                    },
                                    previous=auth["revision"],
                                )
                        refused = await client.post(
                            f"/api/workbench/capability-calls/{call['id']}/decision",
                            json=approval(call).model_dump(mode="json"),
                        )
                        assert refused.status_code == 409, refused.text
                    else:
                        qualified.remove(
                            ctx.experts[0].reference if nested else ctx.leaf.reference
                        )
                    await drive(runner)
                    assert len(ctx.remote.calls) == 1
                    break
                answer = await client.post(
                    f"/api/workbench/capability-calls/{call['id']}/decision",
                    json=approval(call).model_dump(mode="json"),
                )
                assert answer.status_code == 200, answer.text
            await drive(runner)
            async with db() as s:
                final = (await WorkbenchRepository(s).get_task(task))["state"]
            assert final == ("FAILED" if revoke else "SUCCEEDED")
            assert len(ctx.remote.calls) == (1 if revoke else steps)
            assert (await client.put(path + "/execution", json=body)).json() == receipt
            finished = (await client.get(path)).json()
            assert finished["execution_state"] == final
            assert finished["delivery"] == (
                None
                if revoke
                else {
                    "summary_receipt"
                    if nested
                    else "final_receipt": "test@example.test"
                }
            )
            if nested and not revoke:
                async with db() as s:
                    repo = WorkbenchRepository(s)
                    stored = await repo.get_task(task)
                    assert stored["pack_snapshot"]["scope_hours"] == view["scope_hours"]
                    deadline = stored["created_at"] + timedelta(
                        hours=view["scope_hours"]
                    )
                    for name in ("first", "second"):
                        row = await repo.call_by_key(
                            ctx.owner, call["session_id"], node_key(task, [name])
                        )
                        assert row["detail"]["exports"]["outputs"] == {
                            "final_receipt": "test@example.test"
                        }
                        assert row["status"] == "succeeded"
                        scope = await repo.method_authorization(ctx.owner, row["id"])
                        assert datetime.fromisoformat(scope["expires_at"]) == deadline
                        child = await repo.call_by_key(
                            ctx.owner,
                            call["session_id"],
                            node_key(task, [name, "first"]),
                        )
                        assert child["parent_call_id"] == row["id"]
            assert len(model.turn_inputs) == 1
        finally:
            if runner is not match[3]:
                await close(runner)


@asynccontextmanager
async def selected_match(
    db,
    monkeypatch,
    expert,
    qualified,
    owner,
    make_capabilities,
    *,
    token=None,
    library=None,
):
    class Market:
        async def planning_formal_refs(self, subject, refs):
            assert subject == "author"
            return qualified.intersection(MethodRef.model_validate(r) for r in refs)

    def factory(repo):
        caps = make_capabilities(repo)
        caps.formal_sources.market = Market()
        return caps

    ref = expert.reference.model_dump(mode="json")
    promise, statement = next(
        iter(expert.draft.definition.capability_declaration.promises.items())
    )
    proposal = {
        "contract": "knowbench/expert-match/v5",
        "requirements": [
            {
                "id": "r",
                "kind": "result",
                "statement": "Return a receipt",
                "question_quote": "receipt",
                "required": True,
            }
        ],
        "outcome": "candidates",
        "picks": [
            {
                "reference": ref,
                "reason": "Notice",
                "coverage": [
                    {"requirement": "r", "promise": promise, "quote": statement}
                ],
                "limitations": [],
            }
        ],
        "explanation": "One candidate",
        "clarification": None,
    }
    model = ScriptedAppServer([json.dumps(proposal)])
    await model.start()
    runner = runner_for(db)
    runner.capabilities = factory
    try:
        async with db() as s:
            repo = WorkbenchRepository(s)
            async with repo.transaction():
                await s.execute(
                    text(
                        "update workbench_sandboxes set app_server_url=:url,token_ref='inline:t'"
                    ),
                    {"url": f"ws://127.0.0.1:{model.port}"},
                )
                env = await s.scalar(
                    text(
                        "select id from workbench_environments where owner_actor_id=:owner limit 1"
                    ),
                    {"owner": owner},
                )
                project = await repo.create_project(
                    owner_actor_id=owner,
                    environment_id=str(env),
                    workspace_root="/home/u/research",
                    path="ordinary",
                    title="Ordinary",
                )
        app = web_app(db, None, monkeypatch)

        def finder(session):
            repo = WorkbenchRepository(session, browser_view=True)
            capabilities = factory(repo)
            return FindExpertService(
                repo,
                formal_registry=capabilities.formal_sources.registry,
                material_read_enabled=capabilities.data_function is not None,
                script_enabled=capabilities.script_executor is not None,
                instruction_service=capabilities.instruction_service,
            )

        monkeypatch.setattr(routes, "find_expert_service", finder)

        async def service():
            async with db() as s:
                yield factory(WorkbenchRepository(s, browser_view=True))

        app.dependency_overrides[capability_routes.service] = service
        app.dependency_overrides[capability_routes.method_read_service] = service
        app.dependency_overrides[routes.get_web_knowbase_token_provider] = lambda: token
        if library:
            monkeypatch.setattr(routes, "knowbase_v2", lambda: library)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/api/workbench/projects/{project}/find-expert",
                json={
                    "reference": ref,
                    "question": "Please return a receipt",
                    "idempotency_key": "normal-expert-match",
                    "match_contract": "knowbench/expert-match/v5",
                },
            )
            assert response.status_code == 201, response.text
            match = response.json()["task_id"]
            path = "/api/workbench/find-expert/" + match
            await drive(runner)
            result = (await client.get(path)).json()
            assert "formal" in result, result
            selected = {
                "reference": ref,
                "review_digest": result["formal"]["review_digest"],
            }
            assert (
                await client.put(path + "/selection", json=selected)
            ).status_code == 200
            yield client, path, selected, runner, factory, model
    finally:
        await close(runner)
        await model.close()
