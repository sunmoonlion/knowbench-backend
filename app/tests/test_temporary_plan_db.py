"""An empty authorized expert directory leads to a recoverable plan using a purchased tool."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

from formal_plan_support import setup_plan
from formal_recursive_support import register_completed
from test_capabilities_db import approval
from test_ordinary_expert_execution_db import selected_match
from test_workbench_advisor_db import drive
from test_workbench_ledger_db import db as db

from app.domain.workbench.errors import StaleStateVersion
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_empty_intake_temporary_plan_runs_only_after_confirmation(
    db, monkeypatch
):
    ctx = await setup_plan(db)
    await register_completed(db, ctx)
    ctx.remote.calls.clear()
    desk = AsyncMock()
    desk.submit.return_value = {"request_id": str(uuid4()), "handoff": "new"}
    qualified = {ctx.leaf.reference}
    choice = {
        "outcome": "plan",
        "steps": [ctx.leaf.reference.model_dump(mode="json")],
        "explanation": "Use the qualified receipt tool.",
    }
    async with selected_match(
        db,
        monkeypatch,
        ctx.leaf,
        qualified,
        ctx.owner,
        ctx.factory,
        planning_choice=choice,
        incubations=desk,
    ) as match:
        client, path, selection, runner, factory, model = match
        review = {"review_digest": selection["review_digest"]}
        assert not model.turn_inputs and not ctx.remote.calls
        p = path + "/temporary-plan"
        a, b = await asyncio.gather(*(client.put(p, json=review) for _ in range(2)))
        assert a.status_code == b.status_code == 200 and a.json() == b.json(), a.text
        await drive(runner)
        response = await client.get(p)
        assert response.status_code == 200, response.text
        sketch = response.json()
        assert sketch["outcome"] == "plan" and len(sketch["scope"]["steps"]) == 1, (
            sketch
        )
        assert len(model.turn_inputs) == 1 and not ctx.remote.calls
        decision = {**review, "sketch_digest": sketch["sketch_digest"]}
        for _ in range(2):
            response = await client.put(p + "/adoption", json=decision)
            assert response.status_code == 200, response.text
        assert (
            await client.put(
                p + "/saved", json={"review_digest": sketch["sketch_digest"]}
            )
        ).status_code == 200
        prep = (await client.get(path + "/preparation")).json()
        bindings = {
            **review,
            "expected_revision": 0,
            "budget_limit": "1",
            "values": {"to": "test@example.test"},
            "accounts": {k: v[0]["pin"] for k, v in prep["account_options"].items()},
            "materials": {},
        }
        response = await client.put(path + "/preparation", json=bindings)
        assert response.status_code == 200, response.text
        view = (await client.get(path + "/execution-preview?revision=1")).json()
        assert not view["issues"] and not view["dispatch_gap"], view
        assert not ctx.remote.calls
        ack = await client.put(
            path + "/execution",
            json={"revision": 1, "preview_digest": view["preview_digest"]},
        )
        assert ack.status_code == 200, ack.text
        tid = ack.json()["task_id"]
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            attempts = await repo.list_attempts(tid)
            calls = [
                await repo.capability_call(ctx.owner, a["refs"]["capability_call_id"])
                for a in attempts
            ]
        assert len(calls) == 1 and not ctx.remote.calls
        res = await client.post(
            f"/api/workbench/capability-calls/{calls[0]['id']}/decision",
            json=approval(calls[0]).model_dump(mode="json"),
        )
        assert res.status_code == 200, res.text
        await drive(runner)
        result = (await client.get(path)).json()
        assert result["delivery"] == {"receipt": "test@example.test"}, result
        assert len(model.turn_inputs) == 1 and len(ctx.remote.calls) == 1

        audit = (await client.get(p + "/audit")).json()
        assert (
            len(audit["rounds"]) == 1
            and audit["rounds"][0]["catalog"]["trace"][0]["by"] == "host"
        )
        assert audit["execution"][
            "artifact_id"
        ] and "test@example.test" not in json.dumps(audit)
        suggestion = result["feedback"]["suggested_plan"]
        assert suggestion == {
            "sketch_digest": sketch["sketch_digest"],
            "methods": choice["steps"],
            "outputs": ["receipt"],
        }
        command = {
            **review,
            "gap": "Add source references",
            "plan_digest": sketch["sketch_digest"],
        }
        for _ in range(2):
            handoff = await client.put(path + "/handoff", json=command)
            assert handoff.status_code == 200, handoff.text
        assert (await client.get(path)).json()["feedback"]["handoff"] == handoff.json()
        sent = desk.submit.await_args.args[0]
        assert sent[
            "suggested_plan"
        ] == suggestion and "test@example.test" not in json.dumps(sent)
        desk.submit.assert_awaited_once()
        original = result["task_id"]
        response = await client.post(
            "/api/workbench/projects/" + result["project_id"] + "/find-expert",
            json={
                "retain_empty": True,
                "idempotency_key": "a-second-run",
                "question": "Another receipt",
                "match_contract": "knowbench/expert-match/v5",
            },
        )
        second = "/api/workbench/find-expert/" + response.json()["task_id"]
        digest = (await client.get(second)).json()["formal"]["review_digest"]
        again = await client.put(
            second + "/temporary-plan/adoption",
            json={
                "review_digest": digest,
                "sketch_digest": sketch["sketch_digest"],
                "source_task": original,
            },
        )
        assert again.status_code == 200, again.text
        assert not (await client.get(second + "/preparation")).json()["revision"]
        assert len(model.turn_inputs) == 1 and len(ctx.remote.calls) == 1


async def test_clarification_requires_separately_authorized_pinned_probe(
    db, monkeypatch
):
    ctx = await setup_plan(db)
    await register_completed(db, ctx)
    library = AsyncMock()
    pin = {"material_id": str(uuid4()), "version_no": 3, "sha256": "d" * 64}
    header = {"sheet": "", "column": "transaction_id"}
    library.check_planning_column.return_value = {**pin, **header, "status": "unknown"}
    library.preparation_material.return_value = ("ledger", {(3, pin["sha256"])})
    token = AsyncMock(return_value="user-session")
    clarification = {
        "outcome": "clarify",
        "steps": [],
        "explanation": "Is the transaction identifier present?",
    }
    async with selected_match(
        db,
        monkeypatch,
        ctx.leaf,
        {ctx.leaf.reference},
        ctx.owner,
        ctx.factory,
        planning_choice=clarification,
        token=token,
        library=library,
    ) as match:
        client, path, selection, runner, _, model = match
        p = path + "/temporary-plan"
        review = {"review_digest": selection["review_digest"]}
        r = await client.put(p, json=review)
        assert r.status_code == 200, r.text
        child = r.json()["planning_task"]
        cmd = {**pin, **header, **review, "planning_task": child}
        assert (await client.put(p + "/probe", json=cmd)).status_code == 409
        await drive(runner)
        library.check_planning_column.assert_not_awaited()
        # A failed read commits the permission and allows only an identical retry.
        library.check_planning_column.side_effect = StaleStateVersion(
            "source unavailable"
        )
        assert (await client.put(p + "/probe", json=cmd)).status_code == 409
        assert (
            await client.put(p + "/probe", json={**cmd, "column": "extra"})
        ).status_code == 409
        library.check_planning_column.assert_awaited_once()
        library.check_planning_column.side_effect = None
        library.check_planning_column.reset_mock()
        first, second = await asyncio.gather(
            *(client.put(p + "/probe", json=cmd) for _ in range(2))
        )
        assert first.status_code == 200 and first.json() == second.json(), first.text
        library.check_planning_column.assert_awaited_once()
        assert (
            await client.put(p + "/probe", json={**cmd, "column": "extra"})
        ).status_code == 409
        assert (await client.get(p)).json()["probe"]["result"]["status"] == "unknown"
        resume = {**review, "previous": child, "probe_digest": first.json()["consent"]}
        library.preparation_material.return_value = (None, set())
        assert (await client.put(p, json=resume)).status_code == 409
        library.preparation_material.return_value = ("ledger", {(3, pin["sha256"])})
        model.replies.append(json.dumps(clarification))
        r = await client.put(p, json=resume)
        assert r.status_code == 200, r.text
        await drive(runner)
        assert (await client.get(p)).json()["remaining_rounds"] == 0
        assert len(model.turn_inputs) == 2 and "transaction_id" in str(
            model.turn_inputs[-1]
        )
        assert (
            await client.put(
                p,
                json={
                    **review,
                    "previous": r.json()["planning_task"],
                    "clarification": "retry",
                },
            )
        ).status_code == 409


async def test_unsupported_requirement_stops_before_model_or_catalog(db, monkeypatch):
    ctx = await setup_plan(db)
    await register_completed(db, ctx)
    async with selected_match(
        db,
        monkeypatch,
        ctx.leaf,
        {ctx.leaf.reference},
        ctx.owner,
        ctx.factory,
        planning_choice={"outcome": "gap", "steps": [], "explanation": "missing"},
    ) as match:
        client, path, selection, _, _, model = match
        response = await client.put(
            path + "/temporary-plan",
            json={
                "review_digest": selection["review_digest"],
                "required_capabilities": ["future_operation"],
            },
        )
        assert (
            response.json()["missing"] == ["future_operation"]
            and response.json()["catalog_queries"] == 0
        )
        assert not model.turn_inputs


async def test_model_requests_contract_search_and_runner_change_blocks_adoption(
    db, monkeypatch
):
    from app.interfaces.http.web import workbench_expert as routes

    ctx = await setup_plan(db)
    await register_completed(db, ctx)
    pick = {
        "outcome": "clarify",
        "steps": [],
        "explanation": "Inspect the receipt contract",
        "search_goal": "receipt",
    }
    async with selected_match(
        db,
        monkeypatch,
        ctx.leaf,
        {ctx.leaf.reference},
        ctx.owner,
        ctx.factory,
        planning_choice=pick,
    ) as match:
        client, path, selection, runner, _, model = match
        p = path + "/temporary-plan"
        review = {"review_digest": selection["review_digest"]}
        response = await client.put(p, json={**review, "catalog_mode": "directory"})
        assert response.status_code == 200, response.text
        await drive(runner)
        child = response.json()["planning_task"]
        actual = {
            "outcome": "plan",
            "steps": [ctx.leaf.reference.model_dump(mode="json")],
            "explanation": "Use the exact method",
        }
        model.replies.append(json.dumps(actual))
        result = await client.put(p, json={**review, "previous": child})
        assert result.status_code == 200, result.text
        await drive(runner)
        result = (await client.get(p)).json()
        assert result["outcome"] == "plan" and result["catalog_queries"] == 2, result
        log = (await client.get(p + "/audit")).json()
        assert log["rounds"][1]["catalog"]["trace"][-1]["by"] == "model"
        assert len(model.turn_inputs) == 2
        original = routes.find_expert_service

        def switched(session):
            service = original(session)
            service.script_enabled = not service.script_enabled
            return service

        monkeypatch.setattr(routes, "find_expert_service", switched)
        assert (await client.get(p)).json()["outcome"] == "gap"
        assert (
            await client.put(
                p + "/adoption",
                json={**review, "sketch_digest": result["sketch_digest"]},
            )
        ).status_code == 409


def test_untagged_hard_requirements_and_superfluous_steps():
    import pytest
    from test_formal_expert_intake_db import Registry

    from app.domain.workbench.formal_method_generated import (
        MethodRef,
    )
    from app.domain.workbench.planning_capabilities import requirements
    from app.domain.workbench.temporary_plan import Choice, root_for

    assert requirements("务必独立复核，然后按对象逐项计算") == [
        "independent_review",
        "object_batch",
    ]
    assert requirements("必须并行执行两份工作") == ["parallel_dispatch"]
    registry = Registry()
    docs = list(registry.entries.values())
    refs = [MethodRef.model_validate(row["reference"]) for row in docs]
    assert len(refs) >= 2
    # Complete graphs with unrelated inputs and outputs cannot be stacked without a binding.
    versions = {ref: registry.definitions[ref] for ref in refs}
    with pytest.raises(ValueError):
        root_for(
            str(uuid4()),
            "give one output",
            Choice(outcome="plan", steps=refs, explanation="Extra steps"),
            versions,
        )
