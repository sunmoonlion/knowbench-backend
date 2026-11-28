"""Registered host function through the approval ledger and private method freeze."""

import json
from uuid import uuid4

import httpx
import pytest
from formal_plan_support import freeze, plan_request
from joserfc import jwt
from joserfc.jwk import ECKey
from method_access_support import ACCESS
from test_capabilities_db import OWNER, Remote
from test_formal_execution_db import approve, settle, status
from test_formal_host_calls_db import FORMAL_OPERATION, host_service
from test_formal_method_admission_db import setup as admission_setup
from test_service_function_management_db import pin, revision
from test_source_licenses import license_record
from test_workbench_ledger_db import db as db
from test_workbench_ledger_db import seed

from app.application.workbench.data_function import DataFunction
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.test_runs import KnowbotTestConductor
from app.application.workbench.tokens import TokenIssuer
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError, digest
from app.domain.workbench.capability_contract import Call
from app.domain.workbench.data_function import descriptor, destination, registration
from app.domain.workbench.formal_execution import FORMAL_RUNTIME
from app.domain.workbench.formal_method_admission_generated import FormalMethodSource
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.native_functions import native_registration
from app.domain.workbench.skill_execution import public_call
from app.domain.workbench.tool_operations import ResourceBinding
from app.infrastructure.external.knowbase_data_v2 import KnowbaseDataV2
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.infrastructure.workbench.service_functions import register_revision


def test_input_listing_rejects_a_fact_read_permission():
    wrong = revision(
        name="list_inputs",
        scenes=["native", "step"],
        required_permissions=["facts.read"],
        input_schema={"type": "object", "properties": {"scope": {"enum": ["run"]}}},
    )
    with pytest.raises(CapabilityError):
        native_registration(wrong)
    with pytest.raises(CapabilityError):
        descriptor(wrong)


async def test_approved_host_function_freezes_with_exact_provider_receipt(db):
    rev = revision(
        input_schema={
            "type": "object",
            "properties": {
                "scope": {"enum": ["market", "run"]},
                "run": {"type": "string"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {"facts": {"type": "array"}},
            "required": ["facts"],
        },
    )
    signer = ECKey.generate_key("P-256")
    issuer = TokenIssuer(signer.as_pem(private=True).decode())
    await exercise_function(db, rev, issuer, ["row-1"])

    from app.application.workbench.tool_registry import formal_registration
    from app.domain.workbench.registry_views import FormalRegistration

    async with db() as session:
        repo = WorkbenchRepository(session)
        inventory = await repo.registry_inventory(OWNER)
        [summary] = [
            FormalRegistration.model_validate(row)
            for row in inventory["formal"]
            if row["source_kind"] == "service_function"
        ]
        exact = await formal_registration(repo, OWNER, summary.reference)
        assert (
            exact.reference == summary.reference
            and exact.source_digest == summary.source_digest
        )
        assert exact.source_revision == 1
        assert summary.source_revision == exact.source_revision


async def exercise_function(
    db,
    rev,
    issuer,
    expected,
    *,
    forward=None,
    grant=None,
    query=None,
    output_name="facts",
):
    arguments = {"scope": "run", **(query or {})}
    admission, _ = await admission_setup(db)
    env, _, _ = await seed(db)
    async with db() as s:
        await register_revision(s, rev)
    record = license_record(pin(rev))
    source = FormalMethodSource(
        **pin(rev), asset_id=None, asset_revision=None, license_basis=record.basis
    )
    tool = registration(source, descriptor(rev))
    op = FORMAL_OPERATION.model_copy(
        update={
            "operation": rev.name,
            "service_url": destination(rev.name),
            "tool_name": tool.name,
            "effect": "read",
            "descriptor_digest": tool.descriptor_digest,
            "resources": (
                ResourceBinding(resource_role="scope", argument_path=("scope",)),
            ),
        }
    )
    doc = admission.model_dump(mode="json", by_alias=True)
    draft = doc["draft"]
    draft["source"] = source.model_dump(mode="json")
    definition = draft["definition"]
    proc = definition["procedure"]
    slot = proc["inputs"]["to"]
    proc.update(
        implementation=pin(rev) | {"requirement_ids": [ACCESS["requirement_id"]]},
        inputs={
            name: slot | {"schema": rev.contract.input_schema["properties"][name]}
            if name == "refs"
            else slot
            for name in arguments
        },
        outputs={
            name: slot | {"schema": schema}
            for name, schema in rev.contract.output_schema["properties"].items()
        },
    )
    definition["how_to_validate"][0]["outputs"] = [output_name]
    definition["access_requirements"] = [
        ACCESS
        | {
            "operations": [rev.name],
            "resources": [
                {
                    "resource_role": "scope",
                    "resource_kind": "connector",
                    "values": ["run"],
                }
            ],
            "data_transfer": {
                "destination": destination(rev.name),
                "argument_fields": list(arguments),
                "purpose": "Query this test's facts",
            },
        }
    ]
    case = {
        "case_id": "query",
        "input": {"values": arguments},
        "repetitions": 1,
        "minimum_passes": 1,
        "expected": {
            "outcome": "succeeded",
            "assertions": [
                {"kind": "equals", "pointer": "/" + output_name, "value": expected}
            ],
        },
    }
    suite = FormalTestSuiteDraft(suite_id=uuid4(), version=1, cases=[case]).freeze()
    request = FormalMethodExecutionRequest.model_validate(
        doc
        | {
            "contract": FORMAL_RUNTIME,
            "execution": {
                "suite": suite,
                "runner": {
                    "kind": "user",
                    "environment_id": env,
                    "workspace_root": "/home/u/research",
                },
                "budget": {"limit": "1", "currency": "CNY"},
                "timeout_seconds": 60,
                "max_wait_seconds": 120,
            },
        }
    )
    scope_ref = f"{request.run_id}:query:1"
    sent = []

    async def provider(req):
        assert req.url.path == f"/api/internal/v1/functions/{rev.name}/1"
        body = json.loads(req.content)
        claims = jwt.decode(req.headers["authorization"].split()[1], issuer.key).claims
        assert claims["sub"] == "author" and claims["aud"] == "knowbase-function"
        assert (
            claims["call_id"] == body["call_id"]
            and claims["function_digest"] == rev.digest == body["digest"]
        )
        assert claims["arguments_digest"] == digest(body["arguments"])
        assert body["arguments"] == arguments | {"run": scope_ref}
        sent.append(body)
        if forward:
            return await forward(req)
        return httpx.Response(
            200,
            json={
                "call_id": body["call_id"],
                "digest": rev.digest,
                "output": {"facts": ["row-1"]},
            },
        )

    client = KnowbaseDataV2(
        {"KNOWBASE_INTERNAL_URL": "https://127.0.0.1"},
        transport=httpx.MockTransport(provider),
    )

    def factory(repo):
        svc = host_service(repo, record, Remote(), operations=(op,))
        svc.data_function = DataFunction(client, issuer)
        return svc

    conductor = KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory)
    async with db() as s:
        repo = WorkbenchRepository(s)
        await FormalExecutions(repo, factory(repo)).accept(request, caller="knowlab")
    if grant:
        await grant(scope_ref)
    await conductor.advance(str(request.run_id))
    pending = await status(db, request, factory)
    assert pending["actions"], pending
    assert not sent
    await approve(db, request, factory, pending)
    await settle(conductor, request)
    result = await status(db, request, factory)
    assert result["status"] == "passed", result
    assert len(sent) == 1
    leaf = freeze(request, result)
    async with db() as s:
        repo = WorkbenchRepository(s)
        call = await repo.capability_call(OWNER, sent[0]["call_id"])
        assert (
            Call.model_validate(public_call(call)).detail.execution_kind
            == "formal_function"
        )
        await factory(repo).formal_sources.register(leaf, caller="knowlab")

    # Fixed expert with two visits to the same pinned function. Each visit needs
    # its own approval even when both read the same case grant.
    expert = plan_request(
        request,
        leaf,
        case,
        input_name="scope",
        output_name=output_name,
        repeat_input=True,
    )
    scope_ref = f"{expert.run_id}:expert:1"
    async with db() as s:
        repo = WorkbenchRepository(s)
        await FormalExecutions(repo, factory(repo)).accept(expert, caller="knowlab")
    if grant:
        await grant(scope_ref)
    for count in (1, 2):
        await settle(conductor, expert)
        waiting = await status(db, expert, factory)
        assert len(waiting["actions"]) == 1 and len(sent) == count, waiting
        await approve(db, expert, factory, waiting)
    await settle(conductor, expert)
    complete = await status(db, expert, factory)
    assert complete["status"] == "passed", complete
    assert len({body["call_id"] for body in sent}) == 3
    async with db() as s:
        repo = WorkbenchRepository(s)
        frozen = freeze(expert, complete)
        await factory(repo).formal_sources.register(frozen, caller="knowlab")
