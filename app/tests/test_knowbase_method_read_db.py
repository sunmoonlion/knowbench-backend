"""Host and model incubation reads share owner consent for an exact invocation."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from formal_plan_support import advance, freeze
from formal_recursive_support import accept_request
from joserfc import jwt
from joserfc.jwk import ECKey
from test_capabilities_db import Remote
from test_formal_execution_db import approve
from test_formal_host_calls_db import FORMAL_OPERATION
from test_formal_method_admission_db import setup as admission_setup
from test_formal_method_admission_db import validation_request
from test_source_licenses import license_record
from test_workbench_ledger_db import OWNER, seed
from test_workbench_ledger_db import db as db

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.formal_methods import FormalMethods
from app.application.workbench.knowbase_method_read import (
    TOOL,
    pending_read,
)
from app.application.workbench.test_runs import KnowbotTestConductor
from app.application.workbench.tokens import TokenIssuer, generate_private_key_pem
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.formal_execution import FORMAL_RUNTIME
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.domain.workbench.tool_operations import ResourceBinding
from app.domain.workbench.tool_registry import ToolRegistration
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.endpoints.capability_routes import (
    PinnedParentApproval,
    approve_knowbase_read,
)


async def owner_token():
    return "owner-token"


def pinned_approval(subject_digest):
    return PinnedParentApproval(
        subject_digest=subject_digest,
        sha256=hashlib.sha256(b"verified").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


class DataPeer:
    material_id = str(uuid4())
    grant_id = str(uuid4())

    def __init__(self, issuer):
        self.key = ECKey.import_key(issuer.public_pem())
        self.consent = None

    async def method_parent_grant(self, token, read, sha256, expires_at):
        assert (
            token == "owner-token" and sha256 == hashlib.sha256(b"verified").hexdigest()
        )
        key = (read["task_id"], read["material_id"], read["version_no"])
        self.parent_limits = getattr(self, "parent_limits", {})
        return self.grant_id, self.parent_limits.setdefault(key, expires_at)

    async def consent_method_read(self, token, grant_id, read, expires_at):
        assert token == "owner-token" and grant_id == self.grant_id
        self.consent = deepcopy(read)

    async def revoke_method_read(self, token, grant_id, call_id):
        assert token == "owner-token" and grant_id == self.grant_id
        assert self.consent["path"][-1]["call_id"] == call_id
        self.consent = None

    async def invoke_data_query(self, name, token, body):
        from app.domain.workbench.capabilities import CapabilityError

        claims = jwt.decode(token, self.key, algorithms=["ES256"]).claims
        assert (
            name == TOOL
            and claims["aud"] == "knowbase-function"
            and claims["sub"] == "author"
        )
        assert (
            claims["call_id"] == body["call_id"]
            and claims["function_digest"] == body["digest"]
        )
        if claims["method_read"] != self.consent:
            raise CapabilityError("KnowBase declined this read")
        assert body["arguments"] == {"material_id": self.material_id, "version_no": 1}
        return {
            "call_id": body["call_id"],
            "digest": body["digest"],
            "output": {
                "text": "verified",
                "source": {
                    "version_no": 1,
                    "material_id": self.material_id,
                    "sha256": hashlib.sha256(b"verified").hexdigest(),
                    "grant_id": self.grant_id,
                    "method_call_id": self.consent["path"][-1]["call_id"],
                },
            },
        }


@pytest.fixture
def reader_peer():
    issuer = TokenIssuer(generate_private_key_pem(), issuer="knowbench")
    return issuer, DataPeer(issuer)


def suite(args, caller="host"):
    return (
        FormalTestSuiteDraft.model_validate(
            {
                "suite_id": str(uuid4()),
                "version": 1,
                "cases": [
                    {
                        "case_id": "read",
                        "caller": caller,
                        "input": {
                            "values": args,
                            **(
                                {"model_prompt": "Read the selected material."}
                                if caller == "model"
                                else {}
                            ),
                        },
                        "repetitions": 1,
                        "minimum_passes": 1,
                        "expected": {
                            "outcome": "succeeded",
                            "assertions": [
                                {
                                    "kind": "equals",
                                    "pointer": "/text",
                                    "value": "verified",
                                }
                            ],
                        },
                    }
                ],
            }
        )
        .freeze()
        .model_dump(mode="json")
    )


def wrap(request, frozen, *, material_slot="material_id"):
    doc = request.model_dump(mode="json", by_alias=True)
    definition = deepcopy(doc["draft"]["definition"])
    interface = frozen.draft.definition.interfaces()
    ref = frozen.reference.model_dump(mode="json")
    definition["procedure"] = {
        "kind": "orchestration",
        "instructions": "Return the approved text",
        "steps": [
            {
                "id": "read",
                "candidates": [ref],
                "select_when": "exact version",
                "required_when": "always",
                "always_required": True,
                "depends_on": [],
                "inputs": {
                    k: {
                        "slot": slot["slot"],
                        "binding": {
                            "source": "caller",
                            "name": material_slot if k == "material_id" else k,
                        },
                    }
                    for k, slot in interface["inputs"].items()
                },
                "outputs": {k: v["slot"] for k, v in interface["outputs"].items()},
                "on_failure": "fail",
            }
        ],
        "exports": {
            k: {"step": "read", "output": k, "always_present": True}
            for k in interface["outputs"]
        },
        "max_depth": 4,
        "max_expansions": 5,
        "max_steps": 3,
        "max_replans": 0,
    }
    if material_slot != "material_id":
        inputs = dict(doc["execution"]["suite"]["cases"][0]["input"]["values"])
        inputs[material_slot] = inputs.pop("material_id")
        doc["execution"]["suite"] = suite(inputs)
    doc.update(
        method_id=str(uuid4()),
        run_id=str(uuid4()),
        workdraft_id=str(uuid4()),
        request_id=str(uuid4()),
        draft={"definition": definition, "source": None, "allowed_callers": ["host"]},
        dependencies=[ref],
    )
    return FormalMethodExecutionRequest.model_validate(doc)


async def read_candidate(db, reader_peer):
    issuer, peer = reader_peer
    from test_service_function_management_db import pin, revision

    from app.application.workbench.data_function import DataFunction
    from app.domain.workbench.data_function import descriptor, destination
    from app.domain.workbench.service_functions import FunctionRevision
    from app.infrastructure.workbench.service_functions import register_revision

    reader = DataFunction(peer, issuer)
    await admission_setup(db, licensed=True)
    env, sandbox, _ = await seed(db)
    remote = Remote()
    rev = (
        FunctionRevision.model_validate(peer.revision)
        if hasattr(peer, "revision")
        else revision(
            name=TOOL,
            required_permissions=["method.inputs.read"],
            scenes=["incubation", "step"],
            input_schema={
                "type": "object",
                "properties": {
                    "material_id": {"type": "string"},
                    "version_no": {"type": "integer"},
                },
                "required": ["material_id", "version_no"],
                "additionalProperties": False,
            },
        )
    )
    url = destination(TOOL)
    registration = ToolRegistration.discover("service_function", url, descriptor(rev))
    async with db() as session:
        await register_revision(session, rev)
    operation = FORMAL_OPERATION.model_copy(
        update={
            "service_url": url,
            "tool_name": TOOL,
            "descriptor_digest": registration.descriptor_digest,
            "operation": TOOL,
            "effect": "read",
            "capability_ref": "knowbase",
            "resources": (
                ResourceBinding(
                    resource_role="material", argument_path=("material_id",)
                ),
            ),
        }
    )
    params = {"material_id": peer.material_id, "version_no": 1}
    async with db() as session:
        repo = WorkbenchRepository(session)
        license = license_record(pin(rev))
        source = (
            await FormalMethods(
                repo, operations=(operation,), source_licenses=(license,)
            ).sources("author")
        )["items"][0]

    def factory(repo):
        return Capabilities(
            repo,
            remote,
            None,
            operations=(operation,),
            source_licenses=(license,),
            data_function=reader,
            runtime_url="http://testserver/api/mcp/capabilities",
        )

    doc = validation_request(source).model_dump(mode="json", by_alias=True)
    definition = doc["draft"]["definition"]
    proc = definition["procedure"]
    slot = proc["inputs"]["to"]
    proc["inputs"] = {
        "material_id": slot,
        "version_no": {**slot, "schema": {"type": "integer"}},
    }
    proc["outputs"] = {"text": slot, "source": {**slot, "schema": {"type": "object"}}}
    proc["implementation"]["requirement_ids"] = ["read"]
    definition["how_to_validate"][0]["outputs"] = ["text", "source"]
    definition["access_requirements"] = [
        {
            "requirement_id": "read",
            "capability_ref": "knowbase",
            "version": operation.version,
            "operations": [TOOL],
            "resources": [
                {
                    "resource_kind": "connector",
                    "resource_role": "material",
                    "values": [peer.material_id],
                }
            ],
            "data_transfer": {
                "destination": url,
                "argument_fields": list(params),
                "purpose": "verify a report",
            },
            "required_when": "always",
            "on_unavailable": "pause",
        }
    ]
    doc.update(
        contract=FORMAL_RUNTIME,
        execution={
            "runner": {
                "kind": "user",
                "environment_id": env,
                "workspace_root": "/home/u/research",
            },
            "budget": {"limit": "1", "currency": "CNY"},
            "timeout_seconds": 60,
            "max_wait_seconds": 120,
            "suite": suite(params),
        },
    )
    ctx = SimpleNamespace(
        request=FormalMethodExecutionRequest.model_validate(doc),
        factory=factory,
        conductor=KnowbotTestConductor(
            workbench_stores(db), formal_capabilities=factory
        ),
    )

    return ctx, remote, sandbox


async def test_read_through_frozen_child_with_separate_owner_consent(db, reader_peer):
    ctx, remote, _ = await read_candidate(db, reader_peer)
    await run_host_tree(db, ctx, reader_peer[1])
    assert remote.calls == []


async def run_host_tree(db, ctx, peer):
    factory = ctx.factory

    previous = None
    versions = []
    for depth in (1, 2, 3):
        await accept_request(db, ctx)
        pending = await advance(db, ctx)
        assert pending["actions"], pending
        action = pending["actions"][0]
        call_id = UUID(action["call_id"])
        async with db() as session:
            svc = factory(WorkbenchRepository(session))
            projection = await pending_read(svc, OWNER, call_id)
            path = projection["read"]["path"]
            assert projection["caller"] == "host"
            assert len(path) == depth and len({n["call_id"] for n in path}) == depth
            assert projection["read"]["material_id"] == peer.material_id
            assert projection["subject_digest"] == action["subject_digest"]
            assert (
                projection["parent_run_ref"]
                == "formal-task:" + projection["read"]["task_id"]
            )
            if previous is not None:
                assert path[1]["method_id"] == str(previous.reference.method_id)
                assert path[1]["definition_digest"] == previous.reference.digest
            await approve_knowbase_read(
                call_id,
                pinned_approval(action["subject_digest"]),
                SimpleNamespace(actor_id=OWNER),
                owner_token,
                svc,
            )
        await approve(db, ctx.request, factory, pending)
        result = await advance(db, ctx)
        assert result["status"] == "passed", result
        previous = freeze(ctx.request, result)
        versions.append(previous)
        async with db() as session:
            await factory(WorkbenchRepository(session)).formal_sources.register(
                previous, caller="knowlab"
            )
        if depth < 3:
            ctx.request = wrap(
                ctx.request,
                previous,
                material_slot="source_document" if depth == 2 else "material_id",
            )
        if depth == 1:
            await check_two_readers(db, ctx)
    return versions


async def check_two_readers(db, ctx):
    from app.application.errors.exceptions import AppException

    doc = ctx.request.model_dump(mode="json", by_alias=True)
    doc.update(run_id=str(uuid4()), method_id=str(uuid4()))
    graph = doc["draft"]["definition"]["procedure"]
    second = deepcopy(graph["steps"][0])
    second.update(id="recheck", depends_on=["read"])
    graph["steps"].append(second)
    for output in graph["exports"].values():
        output["step"] = "recheck"
    expr = SimpleNamespace(
        **{**vars(ctx), "request": FormalMethodExecutionRequest.model_validate(doc)}
    )
    await accept_request(db, expr)
    receipts = []
    for _ in graph["steps"]:
        pending = await advance(db, expr)
        action = pending["actions"][0]
        call_id = UUID(action["call_id"])
        async with db() as session:
            svc = expr.factory(WorkbenchRepository(session))
            shown = await pending_read(svc, OWNER, call_id)
            consent = pinned_approval(action["subject_digest"])
            late = consent.model_copy(
                update={"expires_at": shown["expires_at"] + timedelta(seconds=1)}
            )
            with pytest.raises(AppException) as refused:
                await approve_knowbase_read(
                    call_id, late, SimpleNamespace(actor_id=OWNER), owner_token, svc
                )
            assert refused.value.status_code == 403
            receipts.append(
                await approve_knowbase_read(
                    call_id, consent, SimpleNamespace(actor_id=OWNER), owner_token, svc
                )
            )
        await approve(db, expr.request, expr.factory, pending)
    assert receipts[0]["grant_id"] == receipts[1]["grant_id"]
    assert receipts[0]["method_call_id"] != receipts[1]["method_call_id"]
    assert receipts[0]["expires_at"] == receipts[1]["expires_at"]
    assert (await advance(db, expr))["status"] == "passed"


async def run_host_denial(db, ctx, peer, mode):
    doc = ctx.request.model_dump(mode="json", by_alias=True)
    doc.update(request_id=str(uuid4()), run_id=str(uuid4()))
    case = SimpleNamespace(
        request=FormalMethodExecutionRequest.model_validate(doc),
        factory=ctx.factory,
        conductor=ctx.conductor,
    )
    await accept_request(db, case)
    pending = await advance(db, case)
    action = pending["actions"][0]
    call_id = UUID(action["call_id"])
    async with db() as session:
        svc = ctx.factory(WorkbenchRepository(session))
        shown = await pending_read(svc, OWNER, call_id)
        assert (
            shown["caller"] == "host" and shown["read"]["purpose"] == "verify a report"
        )
        if mode == "revoked":
            consent = await approve_knowbase_read(
                call_id,
                pinned_approval(action["subject_digest"]),
                SimpleNamespace(actor_id=OWNER),
                owner_token,
                svc,
            )
            await peer.revoke_method_read(
                "owner-token", consent["grant_id"], consent["method_call_id"]
            )
    await approve(db, case.request, case.factory, pending)
    result = await advance(db, case)
    assert result["status"] == "failed", result
    async with db() as session:
        call = await WorkbenchRepository(session).capability_call(OWNER, str(call_id))
        assert call["status"] == "failed" and call["detail"]["result"]["isError"]


@pytest.mark.parametrize("mode", ["missing", "revoked"])
async def test_host_read_requires_exact_owner_consent(db, reader_peer, mode):
    ctx, remote, _ = await read_candidate(db, reader_peer)
    await run_host_denial(db, ctx, reader_peer[1], mode)
    assert remote.calls == []


async def run_model_read(db, ctx, sandbox, peer, mode):
    from formal_model_support import ModelServer, model_runner, pending_model_call
    from httpx import ASGITransport, AsyncClient
    from test_capabilities_db import approval
    from test_workbench_task_runtime_db import close

    from app.interfaces.endpoints.capability_routes import (
        get_web_knowbase_token_provider,
        knowbase_v2,
    )

    doc = ctx.request.model_dump(mode="json", by_alias=True)
    doc.update(request_id=str(uuid4()), run_id=str(uuid4()), method_id=str(uuid4()))
    doc["draft"]["allowed_callers"] = ["model"]
    args = {"material_id": peer.material_id, "version_no": 1}
    doc["execution"].update(
        suite=suite(args, "model"), model_probe={"max_turns": 1, "max_method_calls": 1}
    )
    request = FormalMethodExecutionRequest.model_validate(doc)
    fake, runner = ModelServer(), None
    fake.arguments = args
    await fake.start()

    try:
        conductor, runner = await model_runner(db, fake, request, ctx.factory, sandbox)
        fake.app.dependency_overrides[get_web_knowbase_token_provider] = lambda: (
            owner_token
        )
        fake.app.dependency_overrides[knowbase_v2] = lambda: peer
        await conductor.advance(str(request.run_id))
        await runner.run_once()
        _, call = await pending_model_call(db, request)
        assert call is not None, fake.failures
        async with AsyncClient(
            transport=ASGITransport(fake.app), base_url="http://testserver"
        ) as http:
            path = f"/api/workbench/capability-calls/{call['id']}"
            projection = await http.get(path + "/knowbase-read")
            assert projection.status_code == 200, projection.text
            shown = projection.json()
            assert shown["caller"] == "model"
            assert shown["read"]["material_id"] == args["material_id"]
            assert shown["read"]["purpose"] == "verify a report"
            assert shown["read"]["path"][0]["method_id"] == str(request.method_id)
            if mode == "missing":
                refused = await http.post(
                    path + "/knowbase-read",
                    json=pinned_approval("0" * 64).model_dump(mode="json"),
                )
                assert refused.status_code == 403, refused.text
            else:
                result = await http.post(
                    path + "/knowbase-read",
                    json=pinned_approval(shown["subject_digest"]).model_dump(
                        mode="json"
                    ),
                )
                assert result.status_code == 200, result.text
                consent = result.json()
                if mode == "revoked":
                    revoked = await http.delete(
                        f"/api/workbench/knowbase-method-reads/{consent['grant_id']}/{consent['method_call_id']}"
                    )
                    assert revoked.status_code == 204, revoked.text
            response = await http.post(
                path + "/decision", json=approval(call).model_dump()
            )
            assert response.status_code == 200, response.text
        await runner.wait_drivers(timeout=30)
        async with db() as session:
            current = await WorkbenchRepository(session).capability_call(
                OWNER, call["id"]
            )
        assert current["status"] == ("succeeded" if mode == "allowed" else "failed"), (
            current["detail"]
        )
        if mode == "allowed":
            output = current["detail"]["result"]["structuredContent"]
            assert output["text"] == "verified"
            assert (
                output["source"]["method_call_id"]
                == shown["read"]["path"][-1]["call_id"]
            )
        else:
            assert current["detail"]["result"]["isError"]
        assert len(fake.turn_inputs) == len(fake.responses) == 1
        reply = fake.responses[0]["result"]
        assert reply["isError"] == (mode != "allowed")
        delivered = json.loads(reply["content"][0]["text"])
        assert delivered["call_id"] == str(call["id"])
        if mode == "allowed":
            assert delivered["result"]["structuredContent"] == output
            assert json.loads(delivered["result"]["content"][0]["text"]) == output
            trial = SimpleNamespace(
                request=request, conductor=conductor, factory=ctx.factory
            )
            result = await advance(db, trial)
            assert result["status"] == "passed", (
                result.get("admission") or result["cases"]
            )
            async with db() as session:
                receipt = await ctx.factory(
                    WorkbenchRepository(session)
                ).formal_sources.register(freeze(request, result), caller="knowlab")
                assert receipt["status"] == "registered"
        else:
            assert "verified" not in json.dumps(delivered["result"])
    finally:
        if runner:
            await close(runner)
        await fake.close()


@pytest.mark.parametrize("mode", ["allowed", "missing", "revoked"])
async def test_model_read_requires_exact_owner_consent(db, reader_peer, mode):
    ctx, remote, sandbox = await read_candidate(db, reader_peer)
    await run_model_read(db, ctx, sandbox, reader_peer[1], mode)
    assert remote.calls == []
