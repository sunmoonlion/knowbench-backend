"""Qualify every dependency through real local runs; only MCP responses are synthetic."""

from copy import deepcopy
from uuid import uuid4

from formal_plan_support import advance, freeze, setup_plan
from test_formal_execution_db import approve

from app.application.workbench.formal_test_runs import FormalExecutions
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


async def accept_request(db, ctx):
    async with db() as session:
        repo = WorkbenchRepository(session)
        result = await FormalExecutions(repo, ctx.factory(repo)).accept(
            ctx.request, caller="knowlab"
        )
        assert result["status"] == "pending", result


async def complete(db, ctx):
    for _ in range(110):
        result = await advance(db, ctx)
        if not result["actions"]:
            return result
        await approve(db, ctx.request, ctx.factory, result)
    raise AssertionError("Recursive run did not terminate")


async def assert_passed(db, ctx, result):
    if result["status"] == "passed":
        return
    async with db() as session:
        validation = await WorkbenchRepository(session).formal_validation(
            ctx.owner, str(ctx.request.run_id)
        )
        evidence = (validation or {}).get("execution_evidence") or {}
    raise AssertionError(
        {
            "result": result,
            "errors": [item.get("errors") for item in evidence.get("cases", [])],
        }
    )


async def register_completed(db, ctx, *, version=1):
    result = await complete(db, ctx)
    await assert_passed(db, ctx, result)
    document = freeze(ctx.request, result).model_dump(mode="json", by_alias=True)
    document["reference"]["version"] = version
    frozen = FrozenFormalMethod.model_validate(document)
    async with db() as session:
        repo = WorkbenchRepository(session)
        await ctx.factory(repo).formal_sources.register(frozen, caller="knowlab")
        async with repo.transaction():
            await repo.purge_test_run(str(ctx.request.run_id))
    return frozen


def fresh_request(request, *, method_id=None):
    document = request.model_dump(mode="json", by_alias=True)
    document.update(
        request_id=str(uuid4()),
        run_id=str(uuid4()),
        workdraft_id=str(uuid4()),
        method_id=str(method_id or uuid4()),
    )
    return document


def parent_request(base, dependencies, *, depth, expansions, steps):
    document = fresh_request(base)
    definition = deepcopy(document["draft"]["definition"])
    children = []
    for index, frozen in enumerate(dependencies):
        inputs = frozen.draft.definition.interfaces()["inputs"]
        outputs = frozen.draft.definition.interfaces()["outputs"]
        input_name = next(iter(inputs))
        output_name = next(iter(outputs))
        predecessor = children[-1] if children else None
        children.append(
            {
                "id": "first" if index == 0 else "second",
                "candidates": [frozen.reference.model_dump(mode="json")],
                "select_when": "The exact qualified version",
                "required_when": "Always",
                "always_required": True,
                "depends_on": [predecessor["id"]] if predecessor else [],
                "inputs": {
                    input_name: {
                        "slot": inputs[input_name]["slot"],
                        "binding": {
                            "source": "step",
                            "step": predecessor["id"],
                            "output": next(iter(predecessor["outputs"])),
                        }
                        if predecessor
                        else {"source": "caller", "name": "to"},
                    }
                },
                "outputs": {output_name: outputs[output_name]["slot"]},
                "on_failure": "fail",
            }
        )
    last = children[-1]
    definition["procedure"] = {
        "kind": "orchestration",
        "instructions": "Execute the precise qualified dependencies",
        "steps": list(reversed(children)),
        "exports": {
            "summary_receipt": {
                "step": last["id"],
                "output": next(iter(last["outputs"])),
                "always_present": True,
            }
        },
        "max_depth": depth,
        "max_expansions": expansions,
        "max_steps": steps,
        "max_replans": 0,
    }
    definition["how_to_validate"][0]["outputs"] = ["summary_receipt"]
    document["draft"] = {
        "definition": definition,
        "source": None,
        "allowed_callers": ["host"],
    }
    unique = {
        str(f.reference.method_id)
        + ":"
        + str(f.reference.version): f.reference.model_dump(mode="json")
        for f in dependencies
    }
    document["dependencies"] = list(unique.values())
    suite = FormalTestSuiteDraft.model_validate(
        {
            "suite_id": str(uuid4()),
            "version": 1,
            "cases": [
                {
                    "case_id": "recursive",
                    "input": {"values": {"to": "test@example.test"}},
                    "repetitions": 1,
                    "minimum_passes": 1,
                    "expected": {
                        "outcome": "succeeded",
                        "assertions": [
                            {
                                "kind": "equals",
                                "pointer": "/summary_receipt",
                                "value": "test@example.test",
                            }
                        ],
                    },
                }
            ],
        }
    ).freeze()
    document["execution"]["suite"] = suite.model_dump(mode="json")
    document["execution"]["timeout_seconds"] = 600
    return FormalMethodExecutionRequest.model_validate(document)


async def setup_recursive(db, *, variant="expert", accept=True):
    ctx = await setup_plan(db)
    first = await register_completed(db, ctx)
    experts = [first]
    if variant in ("team", "versions", "team_parent"):
        document = fresh_request(
            ctx.request,
            method_id=first.reference.method_id if variant == "versions" else None,
        )
        document["draft"]["definition"]["procedure"]["instructions"] += (
            "; second qualified revision"
        )
        ctx.request = FormalMethodExecutionRequest.model_validate(document)
        await accept_request(db, ctx)
        experts.append(
            await register_completed(db, ctx, version=2 if variant == "versions" else 1)
        )
    else:
        experts.append(first)
    ctx.request = parent_request(ctx.request, experts, depth=3, expansions=7, steps=6)
    ctx.experts = tuple(experts)
    if variant == "team_parent":
        await accept_request(db, ctx)
        team = await register_completed(db, ctx)
        ctx.request = parent_request(
            ctx.request, [team], depth=4, expansions=8, steps=7
        )
        ctx.team = team
    ctx.remote.calls.clear()
    if accept:
        await accept_request(db, ctx)
    return ctx


async def completed_unsealed(db, ctx):
    for _ in range(110):
        pending = await advance(db, ctx)
        assert pending["actions"], pending
        await approve(db, ctx.request, ctx.factory, pending)
        await ctx.conductor.advance(str(ctx.request.run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            rows = await repo.list_test_cases(str(ctx.request.run_id))
            task = await repo.get_task(str(rows[0]["task_id"]))
            done = task["state"] == "SUCCEEDED"
        if done:
            await ctx.conductor.advance(str(ctx.request.run_id))
            async with db() as session:
                repo = WorkbenchRepository(session)
                row = (await repo.list_test_cases(str(ctx.request.run_id)))[0]
                ctx.result = row["result"]
                assert ctx.result["outcome"] == "succeeded"
            return ctx
    raise AssertionError("Recursive run did not complete")
