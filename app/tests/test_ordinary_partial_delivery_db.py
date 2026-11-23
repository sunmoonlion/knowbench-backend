"""An interrupted join can return a pinned branch only with the owner's approval."""

import asyncio
from copy import deepcopy

import pytest
from formal_plan_support import setup_plan
from formal_recursive_support import accept_request, register_completed
from sqlalchemy import text
from test_capabilities_db import approval
from test_ordinary_expert_execution_db import selected_match
from test_workbench_ledger_db import db as db
from test_workbench_task_runtime_db import drive

from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("failure", ["deadline", "withdrawn"])
async def test_partial_receipt_after_interrupted_join(db, monkeypatch, failure):
    ctx = await setup_plan(db, accept=False)
    document = ctx.request.model_dump(mode="json", by_alias=True)
    graph = document["draft"]["definition"]["procedure"]
    step, first = graph["steps"]
    branch = deepcopy(first)
    branch["id"] = "other"
    step["depends_on"] = ["first", "other"]
    graph["steps"].append(branch)
    graph.update(max_steps=3, max_expansions=4)
    ctx.request = type(ctx.request).model_validate(document)
    await accept_request(db, ctx)
    expert = await register_completed(db, ctx)
    qualified = {expert.reference, ctx.leaf.reference}
    async with selected_match(
        db, monkeypatch, expert, qualified, ctx.owner, ctx.factory
    ) as match:
        client, path, selected, runner, _, _ = match
        prep = (await client.get(path + "/preparation")).json()
        result = await client.put(
            path + "/preparation",
            json={
                "expected_revision": 0,
                "review_digest": selected["review_digest"],
                "budget_limit": "1",
                "values": {"to": "test@example.test"},
                "materials": {},
                "accounts": {
                    k: v[0]["pin"] for k, v in prep["account_options"].items()
                },
            },
        )
        result.raise_for_status()
        preview = (await client.get(path + "/execution-preview?revision=1")).json()
        result = await client.put(
            path + "/execution",
            json={"revision": 1, "preview_digest": preview["preview_digest"]},
        )
        result.raise_for_status()
        task = result.json()["task_id"]
        endpoint = f"/api/workbench/tasks/{task}/partial-delivery"
        assert (await client.get(endpoint)).status_code == 404
        await drive(runner)
        async with db() as session:
            repo = WorkbenchRepository(session)
            (attempt,) = await repo.list_attempts(task)
            call = await repo.capability_call(
                ctx.owner, attempt["refs"]["capability_call_id"]
            )
        (
            await client.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json=approval(call).model_dump(mode="json"),
            )
        ).raise_for_status()
        if failure == "withdrawn":
            qualified.remove(ctx.leaf.reference)
        else:
            await drive(runner)
            async with db() as session:
                repo = WorkbenchRepository(session)
                pending = await repo.get_task(task)
                await session.execute(
                    text(
                        "UPDATE workbench_interactions SET expires_at=now()-interval '1 second' WHERE id=:id"
                    ),
                    {"id": pending["active_interaction_id"]},
                )
                await session.commit()
        await drive(runner)
        assert (await client.get(path)).json()["execution_state"] in (
            "FAILED",
            "REJECTED",
        )
        response = await client.get(endpoint)
        response.raise_for_status()
        report = response.json()["report"]
        assert report["received"] == [] and len(report["options"]) == 1
        step = attempt["step_id"]
        assert len(report["missing_steps"]) == 2 and step not in report["missing_steps"]
        assert report["options"][0]["applicable_promises"] == []
        body = {"step": step, "scope_digest": report["scope_digest"]}
        assert (
            await client.put(
                endpoint, json={**body, "step": report["missing_steps"][0]}
            )
        ).status_code == 409
        assert (
            await client.put(
                endpoint, json={**body, "scope_digest": "sha256:" + "0" * 64}
            )
        ).status_code == 409
        results = await asyncio.gather(
            *(client.put(endpoint, json=body) for _ in range(2))
        )
        for r in results:
            r.raise_for_status()
        assert (
            results[0].json()
            == results[1].json()
            == (await client.get(endpoint)).json()
        )
        (receipt,) = results[0].json()["report"]["received"]
        assert receipt["decided_by"] == ctx.owner and receipt["outputs"] == {
            "receipt": "test@example.test"
        }
        assert (await client.get(path)).json()["delivery"] is None
