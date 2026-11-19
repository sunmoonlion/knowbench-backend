"""Formal host, private registration and three-level dispatch with a synthetic data MCP."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from formal_plan_support import advance, freeze
from formal_recursive_support import accept_request
from joserfc import jwt
from joserfc.jwk import ECKey
from test_capabilities_db import Remote, body
from test_formal_execution_db import approve, setup
from test_formal_host_calls_db import FORMAL_OPERATION
from test_formal_method_admission_db import validation_request
from test_source_licenses import license_record
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.capabilities import Capabilities
from app.application.workbench.formal_methods import FormalMethods
from app.application.workbench.knowbase_method_read import (
    TOOL,
    KnowbaseMethodReader,
    pending_read,
)
from app.application.workbench.test_runs import KnowbotTestConductor
from app.application.workbench.tokens import TokenIssuer, generate_private_key_pem
from app.bootstrap.workbench import workbench_stores
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


class DataPeer:
    data_mcp_url = "https://knowbase.example.test/api/mcp/data"
    material_id = str(uuid4())
    grant_id = str(uuid4())

    def __init__(self, issuer):
        self.key = ECKey.import_key(issuer.public_pem())
        self.consent = None

    async def method_parent_grant(self, token, read, sha256, expires_at):
        assert (
            token == "owner-token" and sha256 == hashlib.sha256(b"verified").hexdigest()
        )
        return self.grant_id

    async def consent_method_read(self, token, grant_id, read, expires_at):
        assert token == "owner-token" and grant_id == self.grant_id
        self.consent = deepcopy(read)

    async def data_mcp(self, token, message):
        claims = jwt.decode(token, self.key, algorithms=["ES256"]).claims
        assert claims["tools"] == [TOOL] and claims["sub"] == "author"
        if message["method"] == "tools/list":
            assert "method_read" not in claims
            return {
                "result": {
                    "tools": [
                        {
                            "name": TOOL,
                            "description": "One approved text original",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "material_id": {"type": "string"},
                                    "version_no": {"type": "integer"},
                                },
                                "required": ["material_id", "version_no"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                }
            }
        assert claims["method_read"] == self.consent
        assert message["params"]["arguments"] == {
            "material_id": self.material_id,
            "version_no": 1,
        }
        return {
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": "verified"}],
                "structuredContent": {
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
        }


@pytest.fixture
def reader_peer():
    issuer = TokenIssuer(generate_private_key_pem(), issuer="knowbench")
    return issuer, DataPeer(issuer)


def suite(args):
    return (
        FormalTestSuiteDraft.model_validate(
            {
                "suite_id": str(uuid4()),
                "version": 1,
                "cases": [
                    {
                        "case_id": "read",
                        "input": {"values": args},
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


def wrap(request, frozen):
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
                        "binding": {"source": "caller", "name": k},
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
    doc.update(
        method_id=str(uuid4()),
        run_id=str(uuid4()),
        workdraft_id=str(uuid4()),
        request_id=str(uuid4()),
        draft={"definition": definition, "source": None, "allowed_callers": ["host"]},
        dependencies=[ref],
    )
    return FormalMethodExecutionRequest.model_validate(doc)


async def test_read_through_frozen_child_with_separate_owner_consent(db, reader_peer):
    issuer, peer = reader_peer
    reader = KnowbaseMethodReader(peer, issuer)
    baseline, _, _, _, _ = await setup(db)
    remote = Remote()
    tools = await reader.discover("author")
    registration = ToolRegistration.discover("mcp", reader.url, tools[0])
    operation = FORMAL_OPERATION.model_copy(
        update={
            "service_url": reader.url,
            "tool_name": TOOL,
            "descriptor_digest": registration.descriptor_digest,
            "operation": "read",
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
        service = Capabilities(
            repo, remote, None, operations=(operation,), knowbase_reader=reader
        )
        await service.create_connection(
            OWNER,
            body(key="knowbase-read-connection").model_copy(
                update={"url": reader.url, "token": None}
            ),
        )
        source = (await service.formal_sources.sources("author"))["items"][0]
        license = license_record(source)
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
            knowbase_reader=reader,
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
            "operations": ["read"],
            "resources": [
                {
                    "resource_kind": "connector",
                    "resource_role": "material",
                    "values": [peer.material_id],
                }
            ],
            "data_transfer": {
                "destination": reader.url,
                "argument_fields": list(params),
                "purpose": "verify a report",
            },
            "required_when": "always",
            "on_unavailable": "pause",
        }
    ]
    execution = baseline.execution.model_dump(mode="json")
    execution["suite"] = suite(params)
    doc.update(contract=baseline.contract, execution=execution)
    ctx = SimpleNamespace(
        request=FormalMethodExecutionRequest.model_validate(doc),
        factory=factory,
        conductor=KnowbotTestConductor(
            workbench_stores(db), formal_capabilities=factory
        ),
    )

    async def owner_token():
        return "owner-token"

    previous = None
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
                PinnedParentApproval(
                    sha256=hashlib.sha256(b"verified").hexdigest(),
                    subject_digest=action["subject_digest"],
                    expires_at=datetime.now(UTC) + timedelta(minutes=1),
                ),
                SimpleNamespace(actor_id=OWNER),
                owner_token,
                svc,
            )
        await approve(db, ctx.request, factory, pending)
        result = await advance(db, ctx)
        assert result["status"] == "passed", result
        previous = freeze(ctx.request, result)
        async with db() as session:
            await factory(WorkbenchRepository(session)).formal_sources.register(
                previous, caller="knowlab"
            )
        if depth < 3:
            ctx.request = wrap(ctx.request, previous)
    assert remote.calls == []
