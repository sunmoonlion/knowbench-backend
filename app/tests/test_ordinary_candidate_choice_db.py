"""Frozen input routes take one authorized subtree, in trials and normal delegations."""

from copy import deepcopy
from uuid import uuid4

import test_formal_execution_db
from formal_recursive_support import (
    accept_request,
    parent_request,
    register_completed,
    setup_recursive,
)
from method_access_support import ACCESS
from test_capabilities_db import approval
from test_knowbot_test_runs_db import runner_for
from test_ordinary_expert_execution_db import selected_match
from test_workbench_advisor_db import close, drive
from test_workbench_ledger_db import db as db

from app.domain.workbench.formal_method_execution_generated import FormalTestSuiteDraft
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_frozen_input_selects_one_nested_expert(db, monkeypatch):
    recipients = ["test@example.test", "other@example.test"]
    allowed = deepcopy(ACCESS)
    allowed["resources"][0]["values"] = recipients
    monkeypatch.setattr(test_formal_execution_db, "ACCESS", allowed)
    ctx = await setup_recursive(db, variant="team", accept=False)
    raw = parent_request(
        ctx.request, [ctx.experts[0]], depth=3, expansions=4, steps=3
    ).model_dump(mode="json", by_alias=True)
    refs = [e.reference.model_dump(mode="json") for e in ctx.experts]
    step = raw["draft"]["definition"]["procedure"]["steps"][0]
    step["candidates"] = refs
    step["candidate_choice"] = {
        "input": "to",
        "cases": [
            {"equals": v, "candidate": r} for v, r in zip(recipients, refs, strict=True)
        ],
    }
    raw["dependencies"] = refs
    cases = []
    for index, address in enumerate(recipients):
        case = deepcopy(raw["execution"]["suite"]["cases"][0])
        case["case_id"] = f"route{index}"
        case["input"]["values"]["to"] = address
        case["expected"]["assertions"][0]["value"] = address
        cases.append(case)
    raw["execution"]["suite"] = (
        FormalTestSuiteDraft.model_validate(
            {"suite_id": str(uuid4()), "version": 1, "cases": cases}
        )
        .freeze()
        .model_dump(mode="json")
    )
    ctx.request = type(ctx.request).model_validate(raw)
    await accept_request(db, ctx)
    expert = await register_completed(db, ctx)
    ctx.remote.calls.clear()
    qualified = {expert.reference, ctx.experts[1].reference, ctx.leaf.reference}
    async with selected_match(
        db, monkeypatch, expert, qualified, ctx.owner, ctx.factory
    ) as match:
        client, path, selected, runner, factory, _ = match
        prep = (await client.get(path + "/preparation")).json()
        bindings = {
            "expected_revision": 0,
            "review_digest": selected["review_digest"],
            "budget_limit": "1",
            "accounts": {k: v[0]["pin"] for k, v in prep["account_options"].items()},
            "materials": {},
        }
        for revision, value in enumerate(["missing", recipients[1]], 1):
            r = await client.put(
                path + "/preparation",
                json={
                    **bindings,
                    "expected_revision": revision - 1,
                    "values": {"to": value},
                },
            )
            r.raise_for_status()
            preview = (
                await client.get(path + f"/execution-preview?revision={revision}")
            ).json()
            confirmation = {
                "revision": revision,
                "preview_digest": preview["preview_digest"],
            }
            if revision == 1:
                assert any(
                    i["code"] == "candidate_unmatched" for i in preview["issues"]
                )
                assert (
                    await client.put(path + "/execution", json=confirmation)
                ).status_code == 409
        assert not preview["issues"] and not preview["dispatch_gap"], preview
        assert [n["reference"] for n in preview["subexperts"]] == refs[1:]
        assert preview["choices"] == [
            {
                "step": "first",
                "input": "to",
                "value": recipients[1],
                "reference": refs[1],
            }
        ]
        r = await client.put(path + "/execution", json=confirmation)
        r.raise_for_status()
        task = r.json()["task_id"]
        assert (
            await client.put(path + "/execution", json=confirmation)
        ).json() == r.json()
        for count in (1, 2):
            await drive(runner)
            async with db() as session:
                repo = WorkbenchRepository(session)
                attempts = await repo.list_attempts(task)
                assert len(attempts) == count
                call = await repo.capability_call(
                    ctx.owner, attempts[-1]["refs"]["capability_call_id"]
                )
            (
                await client.post(
                    f"/api/workbench/capability-calls/{call['id']}/decision",
                    json=approval(call).model_dump(mode="json"),
                )
            ).raise_for_status()
        await runner._shutdown()
        resumed = runner_for(db)
        resumed.capabilities = factory
        try:
            await drive(resumed)
        finally:
            await close(resumed)
        assert (await client.get(path)).json()["delivery"] == {
            "summary_receipt": recipients[1]
        }
        assert len(ctx.remote.calls) == 2
