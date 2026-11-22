"""Consumer quote, explicit approval and root settlement through the ordinary API."""

import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from test_capabilities_db import OWNER, approval
from test_formal_instruction_trial_db import fixed_instruction_plan
from test_formal_script_execution_db import (
    Executor,
    RecordedExecutor,
    collect_script,
    mixed_expert,
)
from test_ordinary_expert_execution_db import selected_match
from test_workbench_advisor_db import drive
from test_workbench_ledger_db import db as db

from app.application.workbench.instruction_requests import InstructionRequests
from app.domain.workbench.capabilities import CapabilityError
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("kind", ["script", "instruction"])
@pytest.mark.parametrize("registered", [True, False])
async def test_confirmed_metered_leaf(db, monkeypatch, kind, registered):
    instruction = kind == "instruction"
    executor = (
        RecordedExecutor()
        if not instruction and os.getenv("SCRIPT_TEST_PROVISIONER_URL")
        else Executor()
    )
    if instruction:
        stack, factory, _, leaf, expert, _ = await fixed_instruction_plan(
            db, "complete", registered=registered
        )
        leaves = [leaf]
        values = {"status": "awaiting review"}
    else:
        _, expert, leaves, factory, _ = await mixed_expert(
            db, "success", executor=executor, registered=registered
        )
        values = {"to": "test@example.test"}
    refs = {expert.reference, *(version.reference for version in leaves)}
    enabled = True

    def capabilities(repo):
        service = factory(repo)
        if not enabled:
            service.script_executor = None
            service.instruction_service = None
        return service

    async with selected_match(
        db, monkeypatch, expert, refs, OWNER, capabilities
    ) as match:
        client, path, selected, runner, scoped_factory, _ = match
        if instruction:
            stack.driver.formal = lambda repo: scoped_factory(repo).formal_sources
            runner.instructions = stack.driver
        prep = (await client.get(path + "/preparation")).json()
        bindings = {
            "expected_revision": 0,
            "budget_limit": "0.01",
            "review_digest": selected["review_digest"],
            "values": values,
            "materials": {},
            "accounts": {
                k: v[0]["pin"] for k, v in prep["account_options"].items() if v
            },
        }
        assert (
            await client.put(path + "/preparation", json=bindings)
        ).status_code == 200
        small = (await client.get(path + "/execution-preview?revision=1")).json()
        assert any(i["code"] == "budget_exceeded" for i in small["issues"]), small
        assert (
            await client.put(
                path + "/execution",
                json={"revision": 1, "preview_digest": small["preview_digest"]},
            )
        ).status_code == 409
        assert (
            await client.put(
                path + "/preparation",
                json={**bindings, "expected_revision": 1, "budget_limit": "2"},
            )
        ).status_code == 200
        view = (await client.get(path + "/execution-preview?revision=2")).json()
        assert not view["dispatch_gap"] and not view["issues"], view
        quote = view["steps"][0 if instruction else 1]["quote"]
        assert Decimal(quote["maximum"]) > 0
        if instruction:
            assert quote["fields"] == ["status"] and quote["destination"].endswith(
                "/responses"
            )
        else:
            assert Decimal(quote["per_container_second"]) == Decimal("0.01")
        body = {"revision": 2, "preview_digest": view["preview_digest"]}
        enabled = False
        disabled = (await client.get(path + "/execution-preview?revision=2")).json()
        assert any(i["code"] == "driver_unavailable" for i in disabled["issues"])
        assert (await client.put(path + "/execution", json=body)).status_code == 409
        enabled = True
        if instruction:
            current_quote = InstructionRequests.account_quote

            async def changed_quote(service, owner, session, currency):
                route = await current_quote(service, owner, session, currency)
                return {**route, "as_of": "different tariff revision"}

            with monkeypatch.context() as changed:
                changed.setattr(InstructionRequests, "account_quote", changed_quote)
                refused = await client.put(path + "/execution", json=body)
                assert refused.status_code == 409, refused.text
        response = await client.put(path + "/execution", json=body)
        assert response.status_code == 200, response.text
        task = response.json()["task_id"]
        if instruction:
            stack.units.task = task
            already_sent = len(stack.provider_requests)
        executor.calls.clear()
        for i, step in enumerate(view["steps"]):
            await drive(runner)
            async with db() as session:
                repo = WorkbenchRepository(session)
                attempt = (await repo.list_attempts(task))[-1]
                call = await repo.capability_call(
                    OWNER, attempt["refs"]["capability_call_id"]
                )
            assert call["status"] == "awaiting_approval", call
            if instruction:
                assert len(stack.provider_requests) == already_sent + i
                message = stack.wire["input"][0]["content"][0]
                job = json.loads(message["text"])
                job["arguments"] = call["detail"]["arguments"]
                message["text"] = json.dumps(job)
                stack.units.uid, stack.units.worker = str(uuid4()), str(uuid4())
            result = await client.post(
                f"/api/workbench/capability-calls/{call['id']}/decision",
                json=approval(call).model_dump(mode="json"),
            )
            assert result.status_code == 200, result.text
            if instruction:
                public_quote = result.json()["detail"]["instruction_quote"]
                assert set(public_quote) == {
                    "provider",
                    "model",
                    "destination",
                    "maximum",
                    "currency",
                }
                assert public_quote["destination"] == quote["destination"]
                if i == 0:
                    refs.remove(leaf.reference)
                    async with stack.host() as svc:
                        svc.formal = scoped_factory(svc.repo).formal_sources
                        with pytest.raises(
                            CapabilityError, match="qualification changed"
                        ):
                            await svc.claim(
                                OWNER,
                                task,
                                str(attempt["id"]),
                                caller="formal-expert",
                                credential_id=stack.cred["id"],
                                turn_id="turn-a",
                                body=stack.wire,
                            )
                        checked = await svc.repo.get_attempt(str(attempt["id"]))
                        assert "instruction_request" not in checked["refs"]
                    refs.add(leaf.reference)
                await drive(runner)
                await drive(runner)
            elif step["kind"].endswith("_script"):
                await collect_script(db, scoped_factory)
                assert executor.calls == [call["id"]]
        await drive(runner)
        delivery = (await client.get(path)).json()
        assert delivery["execution_state"] == "SUCCEEDED", delivery
        assert list(delivery["delivery"].values()) == [
            "ready" if instruction else "test@example.test"
        ]
        async with db() as session:
            repo = WorkbenchRepository(session)
            budget = (await repo.get_task(task))["budget"]
            assert Decimal(budget["reserved"]) == 0
            if instruction:
                amounts = [
                    Decimal(a["refs"]["instruction_usage"]["amount"])
                    for a in await repo.list_attempts(task)
                ]
                assert len(stack.provider_requests) == already_sent + 2
                assert Decimal(budget["used"]) == sum(amounts) > 0
            else:
                (call_id,) = executor.calls
                charge = (await repo.script_budget_entries(call_id))["consume"]
                evidence = charge["script_evidence"]
                assert evidence["meter"]["run_id"] == call_id
                assert (
                    Decimal(budget["used"])
                    == charge["amount"]
                    == Decimal(evidence["seconds"])
                    * Decimal(quote["per_container_second"])
                )
                assert charge["amount"] > 0
