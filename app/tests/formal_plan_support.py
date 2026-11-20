"""Real direct-method qualification followed by an expert test; synthetic MCP."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from test_capabilities_db import OWNER, Remote
from test_formal_execution_db import approve, settle, status
from test_formal_execution_db import setup as direct_setup

from app.application.workbench.formal_test_runs import FormalExecutions
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


class EchoRemote(Remote):
    invalid = False

    async def call(self, url, token, name, arguments):
        result = await super().call(url, token, name, arguments)
        return {
            **result,
            "structuredContent": {"receipt": 123 if self.invalid else arguments["to"]},
        }


def freeze(request, result):
    return FrozenFormalMethod.model_validate(
        {
            "contract": "knowlab/frozen-formal-method/v1",
            "owner_subject": request.owner_subject,
            "reference": {
                "method_id": request.method_id,
                "version": 1,
                "digest": request.draft.definition.digest(),
            },
            "draft": request.draft,
            "validation": result["admission"]["receipt"],
            "visibility": "private",
        }
    )


async def setup_plan(db, *, accept=True, public_leaf=False):
    case = {
        "case_id": "echo",
        "input": {"values": {"to": "test@example.test"}},
        "repetitions": 1,
        "minimum_passes": 1,
        "expected": {
            "outcome": "succeeded",
            "assertions": [
                {"kind": "equals", "pointer": "/receipt", "value": "test@example.test"}
            ],
        },
    }
    leaf_request, asset, remote, factory, conductor = await direct_setup(
        db, cases=[case], remote=EchoRemote(), public_review=public_leaf
    )
    await conductor.advance(str(leaf_request.run_id))
    await approve(db, leaf_request, factory, await status(db, leaf_request, factory))
    await settle(conductor, leaf_request)
    result = await status(db, leaf_request, factory)
    assert result["status"] == "passed", result
    leaf = freeze(leaf_request, result)
    if public_leaf:
        leaf = FrozenFormalMethod.model_validate(
            {**leaf.model_dump(mode="json", by_alias=True), "visibility": "public"}
        )
    async with db() as session:
        repo = WorkbenchRepository(session)
        await factory(repo).formal_sources.register(leaf, caller="knowlab")
        async with repo.transaction():
            await repo.purge_test_run(str(leaf_request.run_id))
    request = plan_request(leaf_request, leaf, case)
    remote.calls.clear()
    if accept:
        async with db() as session:
            repo = WorkbenchRepository(session)
            result = await FormalExecutions(repo, factory(repo)).accept(
                request, caller="knowlab"
            )
            assert result["status"] == "pending", result
    return SimpleNamespace(
        request=request,
        leaf=leaf,
        asset=asset,
        remote=remote,
        factory=factory,
        conductor=conductor,
        owner=OWNER,
    )


async def advance(db, ctx):
    for _ in range(12):
        await ctx.conductor.advance(str(ctx.request.run_id))
        result = await status(db, ctx.request, ctx.factory)
        if result["actions"] or result["status"] in ("passed", "failed", "cancelled"):
            return result
    raise AssertionError("Expert did not reach approval or terminal state")


def plan_request(leaf_request, leaf, case):
    document = leaf_request.model_dump(mode="json", by_alias=True)
    definition = deepcopy(document["draft"]["definition"])
    slot = definition["procedure"]["inputs"]["to"]
    output = definition["procedure"]["outputs"]["receipt"]
    steps = []
    for name, binding, depends in (
        ("first", {"source": "caller", "name": "to"}, []),
        ("second", {"source": "step", "step": "first", "output": "receipt"}, ["first"]),
    ):
        steps.append(
            {
                "id": name,
                "candidates": [leaf.reference.model_dump(mode="json")],
                "select_when": "The sole method",
                "required_when": "Always",
                "always_required": True,
                "depends_on": depends,
                "inputs": {"to": {"slot": slot, "binding": binding}},
                "outputs": {"receipt": output},
                "on_failure": "fail",
            }
        )
    definition["procedure"] = {
        "kind": "orchestration",
        "instructions": "Return two approved receipts in sequence",
        "steps": list(reversed(steps)),
        "exports": {
            "final_receipt": {
                "step": "second",
                "output": "receipt",
                "always_present": True,
            }
        },
        "max_depth": 2,
        "max_expansions": 3,
        "max_steps": 2,
        "max_replans": 0,
    }
    definition["how_to_validate"][0]["outputs"] = ["final_receipt"]
    case["case_id"] = "expert"
    case["expected"]["assertions"][0]["pointer"] = "/final_receipt"
    suite = FormalTestSuiteDraft.model_validate(
        {"suite_id": str(uuid4()), "version": 1, "cases": [case]}
    ).freeze()
    document.update(
        run_id=str(uuid4()),
        request_id=str(uuid4()),
        workdraft_id=str(uuid4()),
        method_id=str(uuid4()),
        draft={"definition": definition, "source": None, "allowed_callers": ["host"]},
        dependencies=[leaf.reference.model_dump(mode="json")],
    )
    document["execution"]["suite"] = suite.model_dump(mode="json")
    return FormalMethodExecutionRequest.model_validate(document)
