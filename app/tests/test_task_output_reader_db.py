"""Both caller entries consume the same accepted step through the platform adapter."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from formal_model_support import ModelServer, model_runner, pending_model_call
from formal_plan_support import advance, freeze, setup_plan
from formal_recursive_support import accept_request, complete
from httpx import ASGITransport, AsyncClient
from test_capabilities_db import Remote, approval, body
from test_formal_execution_db import approve, status
from test_formal_host_calls_db import FORMAL_OPERATION
from test_formal_method_admission_db import validation_request
from test_formal_method_use_routes_db import application
from test_knowbase_method_read_db import suite, wrap
from test_source_licenses import license_record
from test_workbench_ledger_db import OTHER, OWNER, seed
from test_workbench_ledger_db import db as db
from test_workbench_task_runtime_db import close

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.task_output_reader import (
    TOOL,
    descriptor,
    selected_output,
)
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import AssetAction, CapabilityError
from app.domain.workbench.errors import NotFound
from app.domain.workbench.formal_execution import FORMAL_RUNTIME
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.tool_operations import ResourceBinding
from app.domain.workbench.tool_registry import ToolRegistration
from app.infrastructure.workbench.repository import WorkbenchRepository

URL = "https://knowbench.example.test/api/mcp/workbench"


async def reader(db):
    producer = await setup_plan(db)
    result = await complete(db, producer)
    assert result["status"] == "passed", result
    args = {
        "task_id": result["cases"][0]["task_id"],
        "name": "formal-step:first",
        "version": 1,
        "output": "receipt",
    }
    remote = Remote()
    policy = FORMAL_OPERATION.model_copy(
        update={
            "service_url": URL,
            "tool_name": TOOL,
            "descriptor_digest": ToolRegistration.discover(
                "mcp", URL, descriptor()
            ).descriptor_digest,
            "operation": "read",
            "effect": "read",
            "capability_ref": "task-output",
            "resources": (
                ResourceBinding(resource_role="task", argument_path=("task_id",)),
            ),
        }
    )
    identity = {
        "kind": "mcp",
        "source": URL,
        "version": policy.version,
        "entry": TOOL,
        "digest": "sha256:" + policy.descriptor_digest,
    }
    licenses = (license_record(identity),)

    def factory(repo):
        return Capabilities(
            repo,
            remote,
            task_outputs_url=URL,
            operations=(policy,),
            source_licenses=licenses,
            runtime_url="http://testserver/api/mcp/capabilities",
        )

    async with db() as session:
        repo = WorkbenchRepository(session)
        svc = factory(repo)
        catalog = svc.formal_sources
        assert (await catalog.sources("author"))["items"] == []
        with pytest.raises(NotFound):
            await catalog.validate(
                validation_request(
                    {
                        **identity,
                        "asset_id": str(uuid4()),
                        "asset_revision": 1,
                        "license_basis": licenses[0].basis,
                    }
                ),
                caller="knowlab",
            )
        asset = await svc.create_connection(
            OWNER,
            body(key="read-platform-output").model_copy(
                update={"url": URL, "token": None}
            ),
        )
        [source] = (await catalog.sources("author"))["items"]
        assert source == {
            **identity,
            "asset_id": asset["id"],
            "asset_revision": asset["revision"],
            "license_basis": licenses[0].basis,
        }
        assert (await catalog.sources("other"))["items"] == []
        for owner, selection in [
            (OTHER, args),
            (OWNER, {**args, "version": 2}),
            (OWNER, {**args, "output": "other"}),
        ]:
            with pytest.raises(CapabilityError):
                await selected_output(repo, owner, selection)
    return args, source, producer.request.execution.runner, factory, remote


async def test_disabled_registration_stops_output_reader_before_approval_and_dispatch(
    db,
):
    args, source, account, factory, remote = await reader(db)
    req = request_for(args, source, account, "host")
    ctx = SimpleNamespace(
        request=req,
        factory=factory,
        conductor=KnowbotTestConductor(
            workbench_stores(db), formal_capabilities=factory
        ),
    )
    await accept_request(db, ctx)
    pending = await advance(db, ctx)
    [action] = pending["actions"]
    async with db() as session:
        svc = factory(WorkbenchRepository(session))
        await svc.action(
            OWNER,
            source["asset_id"],
            AssetAction(action="disable", revision=source["asset_revision"]),
        )
        assert (await svc.formal_sources.sources("author"))["items"] == []
        call = await svc.repo.capability_call(OWNER, action["call_id"])
        with pytest.raises(CapabilityError, match="Installed source changed"):
            await svc.decide(OWNER, call["id"], approval(call))
        stored = await svc.repo.capability_call(OWNER, call["id"])
        assert stored["status"] == "awaiting_approval"
        assert "result" not in stored["detail"]
    assert not remote.calls


def request_for(args, source, runner, caller):
    doc = validation_request(source).model_dump(mode="json", by_alias=True)
    definition = doc["draft"]["definition"]
    proc = definition["procedure"]
    slot = proc["inputs"]["to"]
    proc["inputs"] = {
        k: {**slot, "schema": {"type": "integer" if k == "version" else "string"}}
        for k in args
    }
    proc["outputs"] = {"value": slot, "source": {**slot, "schema": {"type": "object"}}}
    proc["implementation"]["requirement_ids"] = ["read"]
    definition["how_to_validate"][0]["outputs"] = ["value", "source"]
    definition["access_requirements"] = [
        {
            "requirement_id": "read",
            "capability_ref": "task-output",
            "version": FORMAL_OPERATION.version,
            "operations": ["read"],
            "resources": [
                {
                    "resource_kind": "connector",
                    "resource_role": "task",
                    "values": [args["task_id"]],
                }
            ],
            "data_transfer": {
                "destination": URL,
                "argument_fields": list(args),
                "purpose": "verify selected output",
            },
            "required_when": "always",
            "on_unavailable": "pause",
        }
    ]
    doc["draft"]["allowed_callers"] = [caller]
    cases = suite(args, caller)["cases"]
    cases[0]["expected"]["assertions"] = [
        {"kind": "equals", "pointer": "/value", "value": "test@example.test"}
    ]
    doc.update(
        contract=FORMAL_RUNTIME,
        execution={
            "runner": runner.model_dump(mode="json"),
            "budget": {"limit": "1", "currency": "CNY"},
            "timeout_seconds": 60,
            "max_wait_seconds": 120,
            "suite": FormalTestSuiteDraft(suite_id=uuid4(), version=1, cases=cases)
            .freeze()
            .model_dump(mode="json"),
            **(
                {"model_probe": {"max_turns": 1, "max_method_calls": 1}}
                if caller == "model"
                else {}
            ),
        },
    )
    return FormalMethodExecutionRequest.model_validate(doc)


@pytest.mark.parametrize("withdraw", [False, True])
async def test_real_host_and_model_share_pinned_output_and_owner_decision(db, withdraw):
    args, source, account, factory, remote = await reader(db)
    _, sandbox, _ = await seed(db)
    received = []
    for caller in ("host", "model"):
        req = request_for(args, source, account, caller)
        ctx = SimpleNamespace(
            request=req,
            factory=factory,
            conductor=KnowbotTestConductor(
                workbench_stores(db), formal_capabilities=factory
            ),
        )
        fake, runner = None, None
        try:
            if caller == "model":
                fake = ModelServer()
                fake.arguments = args
                await fake.start()
                conductor, runner = await model_runner(db, fake, req, factory, sandbox)
                await conductor.advance(str(req.run_id))
                await runner.run_once()
                _, call = await pending_model_call(db, req)
                assert call is not None, fake.failures
                app = fake.app
            else:
                await accept_request(db, ctx)
                pending = await advance(db, ctx)
                async with db() as session:
                    call = await WorkbenchRepository(session).capability_call(
                        OWNER, pending["actions"][0]["call_id"]
                    )
                app = application(db, factory)
            url = (
                "/api/workbench/capability-calls/" + str(call["id"]) + "/artifact-read"
            )
            async with AsyncClient(
                transport=ASGITransport(app), base_url="http://testserver"
            ) as http:
                preview = await http.get(url)
                assert preview.status_code == 200, preview.text
                data = preview.json()
                assert data["binding"]["return_to"] == caller
                assert data["binding"]["fields"]["value"]["task_id"] == args["task_id"]
                received.append(data["binding"]["fields"]["value"])
                if withdraw:
                    consent = {
                        "scope_digest": data["scope_digest"],
                        "expires_at": data["expires_at"],
                    }
                    assert (await http.post(url, json=consent)).status_code == 200
                    node = data["binding"]["path"][0]["call_id"]
                    assert (
                        await http.put(
                            url + "/" + node,
                            json={"scope_digest": data["scope_digest"], "retain": []},
                        )
                    ).status_code == 200
                    refused = await http.post(
                        url.removesuffix("/artifact-read") + "/decision",
                        json=approval(call).model_dump(),
                    )
                    assert refused.status_code == 403, refused.text
                # Trial actions are approved in KnowLab, including an action initiated by the model.
                pending = await status(db, req, factory)
                await approve(
                    db, req, factory, pending, "decline" if withdraw else "accept"
                )
            if runner:
                await runner.wait_drivers(timeout=30)
                assert fake is not None and len(fake.responses) == 1
                reply = json.loads(fake.responses[0]["result"]["content"][0]["text"])
                assert reply["call_id"] == str(call["id"])
                if withdraw:
                    assert "test@example.test" not in json.dumps(reply)
                else:
                    assert (
                        reply["result"]["structuredContent"]["value"]
                        == "test@example.test"
                    )
            async with db() as session:
                stored = await WorkbenchRepository(session).capability_call(
                    OWNER, str(call["id"])
                )
            assert stored["status"] == ("declined" if withdraw else "succeeded"), (
                stored["detail"]
            )
            if not withdraw:
                assert stored["detail"]["result"]["structuredContent"] == {
                    "source": received[-1],
                    "value": "test@example.test",
                }
                if caller == "model":
                    ctx.conductor = conductor
                result = await advance(db, ctx)
                assert result["status"] == "passed", result
                async with db() as session:
                    repo = WorkbenchRepository(session)
                    validation = await repo.formal_validation(OWNER, str(req.run_id))
                    evidence = validation["execution_evidence"]["cases"][0]
                    assert (
                        evidence["governance"]["artifact_read"]["binding"]
                        == data["binding"]
                    )
                    await factory(repo).formal_sources.register(
                        freeze(req, result), caller="knowlab"
                    )
                if caller == "host":
                    frozen = freeze(req, result)
                    for depth in (2, 3):
                        ctx.request = wrap(ctx.request, frozen)
                        await accept_request(db, ctx)
                        pending = await advance(db, ctx)
                        shown = pending["actions"][0]["subject"]["artifact_read"]
                        assert (
                            shown["fields"]["value"] == received[-1]
                            and len(shown["path"]) == depth
                        )
                        await approve(db, ctx.request, factory, pending)
                        nested = await advance(db, ctx)
                        assert nested["status"] == "passed", nested
                        frozen = freeze(ctx.request, nested)
                        async with db() as session:
                            await factory(
                                WorkbenchRepository(session)
                            ).formal_sources.register(frozen, caller="knowlab")
        finally:
            if runner:
                await close(runner)
            if fake:
                await fake.close()
    assert received[0] == received[1]
    assert not remote.calls
