"""Each object uses one authorized attempt. The collector receives exact keyed results."""

from copy import deepcopy
from uuid import uuid4

import pytest
import test_capabilities_db as connectors
import test_formal_execution_db as direct
import test_formal_method_admission_db as admission
from formal_plan_support import setup_plan
from formal_recursive_support import accept_request, register_completed
from method_access_support import ACCESS
from sqlalchemy import text
from test_capabilities_db import approval
from test_formal_host_calls_db import FORMAL_OPERATION, host_service
from test_knowbot_test_runs_db import runner_for
from test_ordinary_expert_execution_db import selected_match
from test_workbench_ledger_db import db as db
from test_workbench_task_runtime_db import close, drive

from app.domain.workbench.formal_method_execution_generated import FormalTestSuiteDraft
from app.domain.workbench.tool_registry import ToolRegistration
from app.infrastructure.workbench.repository import WorkbenchRepository


async def object_expert(db, monkeypatch, missing):
    addresses = ["test@example.test", "second@example.test"]
    row_schema = {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["object_id", "value"],
            "properties": {
                "object_id": {"type": "string"},
                "value": {"type": "string"},
            },
        },
    }
    tool = deepcopy(connectors.TOOLS[0])
    tool["inputSchema"]["properties"]["rows"] = row_schema
    policy = FORMAL_OPERATION.model_copy(
        update={
            "descriptor_digest": ToolRegistration.discover(
                "mcp", connectors.URL, tool
            ).descriptor_digest
        }
    )
    monkeypatch.setattr(connectors, "TOOLS", [tool])
    monkeypatch.setattr(admission, "OPERATION", policy)
    monkeypatch.setattr(
        direct,
        "host_service",
        lambda repo, record, remote: host_service(
            repo, record, remote, operations=(policy,)
        ),
    )
    ceiling = deepcopy(ACCESS)
    ceiling["resources"][0]["values"] = addresses
    ceiling["data_transfer"]["argument_fields"].append("rows")
    monkeypatch.setattr(direct, "ACCESS", ceiling)
    base = admission.validation_request

    def extended(source):
        req = base(source)
        doc = req.model_dump(mode="json", by_alias=True)
        inputs = doc["draft"]["definition"]["procedure"]["inputs"]
        inputs["rows"] = {
            **inputs["to"],
            "schema": row_schema,
            "required": False,
            "on_missing": "omit",
        }
        return type(req).model_validate(doc)

    monkeypatch.setattr(admission, "validation_request", extended)
    ctx = await setup_plan(db, accept=False)
    doc = ctx.request.model_dump(mode="json", by_alias=True)
    graph = doc["draft"]["definition"]["procedure"]
    collector, worker = graph["steps"]
    worker["inputs"].pop("rows")
    worker["inputs"]["to"]["binding"] = {"source": "object_item", "name": "members"}
    collector["inputs"]["to"]["binding"] = {"source": "literal", "value": addresses[0]}
    collector["inputs"]["rows"]["binding"] = {
        "source": "object_rows",
        "step": worker["id"],
        "output": "receipt",
    }
    collector["inputs"]["rows"]["slot"].update(required=True, on_missing="pause")
    graph.update(
        max_steps=4,
        max_expansions=5,
        object_batch={"max_objects": 3, "on_missing": missing},
    )
    case = doc["execution"]["suite"]["cases"][0]
    case["input"]["values"] = {"members": addresses}
    doc["execution"]["suite"] = (
        FormalTestSuiteDraft.model_validate(
            {"suite_id": str(uuid4()), "version": 1, "cases": [case]}
        )
        .freeze()
        .model_dump(mode="json")
    )
    ctx.request = type(ctx.request).model_validate(doc)
    await accept_request(db, ctx)
    expert = await register_completed(db, ctx)
    ctx.remote.calls.clear()
    return expert, ctx, addresses


@pytest.mark.parametrize(
    ("policy", "expire"), [("stop", False), ("stop", True), ("request_partial", True)]
)
async def test_fixed_list_and_missing_object(db, monkeypatch, policy, expire):
    expert, ctx, original = await object_expert(db, monkeypatch, policy)
    members = list(reversed(original))
    async with selected_match(
        db,
        monkeypatch,
        expert,
        {expert.reference, ctx.leaf.reference},
        ctx.owner,
        ctx.factory,
    ) as match:
        client, path, selected, runner, factory, _ = match
        prep = (await client.get(path + "/preparation")).json()
        saved = await client.put(
            path + "/preparation",
            json={
                "expected_revision": 0,
                "review_digest": selected["review_digest"],
                "budget_limit": "1",
                "accounts": {
                    k: v[0]["pin"] for k, v in prep["account_options"].items()
                },
                "materials": {},
                "values": {"members": members},
            },
        )
        saved.raise_for_status()
        preview = (await client.get(path + "/execution-preview?revision=1")).json()
        assert (
            not preview["issues"]
            and [m["object_id"] for m in preview["object_batch"]["members"]]
            and len(preview["steps"]) == 3
        ), preview
        body = {"preview_digest": preview["preview_digest"], "revision": 1}
        r = await client.put(path + "/execution", json=body)
        r.raise_for_status()
        task = r.json()["task_id"]
        for index in range(3):
            await drive(runner)
            async with db() as session:
                repo = WorkbenchRepository(session)
                attempts = await repo.list_attempts(task)
                assert len(attempts) == index + 1
                call = await repo.capability_call(
                    ctx.owner, attempts[-1]["refs"]["capability_call_id"]
                )
                if expire and index == 1:
                    await session.execute(
                        text(
                            "update workbench_interactions set expires_at=now()-interval '1 second' where id=:id"
                        ),
                        {"id": call["interaction_id"]},
                    )
                    await session.commit()
                    break
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
        result = (await client.get(path)).json()
        assert (await client.put(path + "/execution", json=body)).json() == r.json()
        if not expire:
            assert result["execution_state"] == "SUCCEEDED", result
            assert len(ctx.remote.calls) == 3 and ctx.remote.calls[-1][3]["rows"] == [
                {"object_id": v, "value": v} for v in members
            ]
        else:
            assert (
                result["execution_state"] in ("REJECTED", "FAILED")
                and len(ctx.remote.calls) == 1
            )
            endpoint = f"/api/workbench/tasks/{task}/partial-delivery"
            report = (await client.get(endpoint)).json()["report"]
            if policy == "stop":
                assert report is None
            else:
                (opt,) = report["options"]
                assert opt["object_id"] == ctx.remote.calls[0][3]["to"]
                assert set(report["missing_objects"]) == set(members) - {
                    opt["object_id"]
                }
                receipt = await client.put(
                    endpoint,
                    json={
                        "scope_digest": report["scope_digest"],
                        "step": opt["artifact"]["step_id"],
                    },
                )
                receipt.raise_for_status()
                assert receipt.json()["report"]["received"][0]["outputs"] == {
                    "receipt": opt["object_id"]
                }
