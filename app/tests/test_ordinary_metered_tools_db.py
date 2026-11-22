"""Consumer quote, explicit approval and root settlement through the ordinary API."""

import json
import os
from contextlib import AsyncExitStack
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
from test_workbench_routes import make_client as make_client

from app.application.workbench.instruction_requests import InstructionRequests
from app.domain.workbench.capabilities import CapabilityError
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize(
    ("kind", "registered", "review_case"),
    [
        ("script", True, None),
        ("script", False, None),
        ("instruction", True, None),
        ("instruction", False, None),
        ("instruction", True, "pass"),
        pytest.param(
            "instruction",
            True,
            "live",
            marks=pytest.mark.skipif(
                os.getenv("RUN_CODEX_INSTRUCTION_PROBE") != "1",
                reason="requires pinned local Codex and Docker",
            ),
        ),
        ("instruction", True, "invalid_output"),
        ("instruction", True, "reused_thread"),
    ],
)
async def test_confirmed_metered_leaf(
    db, monkeypatch, kind, registered, review_case, tmp_path, make_client
):
    instruction = kind == "instruction"
    executor = (
        RecordedExecutor()
        if not instruction and os.getenv("SCRIPT_TEST_PROVISIONER_URL")
        else Executor()
    )
    if instruction:
        stack, factory, _, leaf, expert, _ = await fixed_instruction_plan(
            db, "complete", registered=registered, independent=bool(review_case)
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

    async with (
        AsyncExitStack() as workers,
        selected_match(db, monkeypatch, expert, refs, OWNER, capabilities) as match,
    ):
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
        if review_case:
            review = view["steps"][1]
            assert review["independent_review_of"] == "first"
            assert review["from_outputs"] == {
                "status": {"step": ["first"], "output": "summary"}
            }
        if review_case == "pass":
            async with db() as session, session.begin():
                await WorkbenchRepository(session).save_reviewer_preference(
                    OWNER,
                    {
                        "provider": quote["provider"],
                        "model": "unloaded-model",
                        "credential_id": str(stack.cred["id"]),
                    },
                )
            unavailable = (
                await client.get(path + "/execution-preview?revision=2")
            ).json()
            assert any(issue["path"] == ["second"] for issue in unavailable["issues"])
            assert (
                await client.put(
                    path + "/execution",
                    json={"revision": 2, "preview_digest": view["preview_digest"]},
                )
            ).status_code == 409
            async with db() as session, session.begin():
                await WorkbenchRepository(session).save_reviewer_preference(OWNER, None)
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
        private_files = {}
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
                if review_case:
                    stack.units.thread = (
                        "shared"
                        if review_case == "reused_thread"
                        else "ordinary-" + str(i)
                    )
                    if review_case == "invalid_output":
                        stack.units.result = {"summary": 42}
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
                if review_case == "live":
                    from test_instruction_runner_codex import real_ordinary_round

                    capture = await real_ordinary_round(
                        db,
                        make_client.app,
                        monkeypatch,
                        tmp_path / str(i),
                        stack,
                        scoped_factory,
                        task,
                        i + 1,
                        peers=private_files,
                        cleanup=workers,
                    )
                    stack.provider_requests.append(capture)
                else:
                    await drive(runner)
                    await drive(runner)
                if review_case == "reused_thread" and i == 1:
                    await drive(runner)
                    async with db() as session:
                        repo = WorkbenchRepository(session)
                        failed = await repo.get_task(task)
                        assert failed["state"] == "FAILED"
                        producer, reviewer = await repo.list_attempts(task)
                        assert "instruction_request" not in reviewer["refs"]
                        assert Decimal(failed["budget"]["reserved"]) == 0
                        assert Decimal(failed["budget"]["used"]) == Decimal(
                            producer["refs"]["instruction_usage"]["amount"]
                        )
                    assert len(stack.provider_requests) == already_sent + 1
                    return
                if review_case == "invalid_output":
                    await drive(runner)
                    async with db() as session:
                        repo = WorkbenchRepository(session)
                        failed = await repo.get_task(task)
                        assert failed["state"] == "FAILED"
                        (producer,) = await repo.list_attempts(task)
                        assert producer["step_id"] == "first"
                        assert Decimal(failed["budget"]["reserved"]) == 0
                        assert Decimal(failed["budget"]["used"]) == Decimal(
                            producer["refs"]["instruction_usage"]["amount"]
                        )
                    assert len(stack.provider_requests) == already_sent + 1
                    return
                if review_case in ("pass", "live") and i == 1:
                    from app.domain.workbench.instruction_requests import (
                        leaf_responses_body,
                    )

                    outgoing = stack.provider_requests[-1]
                    assert outgoing == leaf_responses_body(
                        {
                            "input": job,
                            "output_schema": stack.wire["text"]["format"]["schema"],
                        },
                        quote["model"],
                        outgoing["max_output_tokens"],
                    )
                    assert json.loads(outgoing["input"][-1]["content"][0]["text"])[
                        "arguments"
                    ] == {"status": "ready"}
                    async with db() as session:
                        first, second = await WorkbenchRepository(
                            session
                        ).list_attempts(task)
                        assert second["refs"]["independent_review"][
                            "attempt_id"
                        ] == str(first["id"])
                        assert (
                            second["refs"]["input_bindings"][0]["id"]
                            == first["output_artifacts"][0]["id"]
                        )
            elif step["kind"].endswith("_script"):
                await collect_script(db, scoped_factory)
                assert executor.calls == [call["id"]]
        await drive(runner)
        delivery = (await client.get(path)).json()
        assert delivery["execution_state"] == "SUCCEEDED", delivery
        assert list(delivery["delivery"].values()) == [
            "ready" if instruction else "test@example.test"
        ]
        if review_case == "live":
            from expert_duty_support import verify_period

            await verify_period(
                db, monkeypatch, client, path, delivery, expert.reference, refs
            )
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
