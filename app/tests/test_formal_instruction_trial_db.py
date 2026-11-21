"""The author trial must reach a metered terminal task before private admission."""

from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_formal_execution_db import settle, status
from test_formal_instruction_db import ARGS
from test_instruction_requests_db import PRICES, ROUTE
from test_instruction_runner_db import drive, stack
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_methods import FormalMethods
from app.application.workbench.formal_test_runs import FormalExecutions
from app.application.workbench.instruction_requests import InstructionRequests
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import TestCleanupPending as CleanupPending
from app.domain.workbench.formal_execution import FORMAL_RUNTIME
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


class AuthorTrial:
    def __init__(self, expected="ready"):
        self.expected = expected

    async def start(self, db, admission, license, environment):
        suite = FormalTestSuiteDraft.model_validate(
            {
                "suite_id": str(uuid4()),
                "version": 1,
                "cases": [
                    {
                        "case_id": "summary",
                        "input": {"values": ARGS},
                        "repetitions": 1,
                        "minimum_passes": 1,
                        "expected": {
                            "outcome": "succeeded",
                            "assertions": [
                                {
                                    "kind": "equals",
                                    "pointer": "/summary",
                                    "value": self.expected,
                                }
                            ],
                        },
                    }
                ],
            }
        ).freeze()
        document = admission.model_dump(mode="json", by_alias=True)
        document.update(
            contract=FORMAL_RUNTIME,
            run_id=str(uuid4()),
            execution={
                "suite": suite.model_dump(mode="json"),
                "runner": {
                    "kind": "user",
                    "environment_id": environment,
                    "workspace_root": "/home/u/research",
                },
                "budget": {"limit": "1", "currency": "CNY"},
                "timeout_seconds": 120,
                "max_wait_seconds": 120,
            },
        )
        self.request = FormalMethodExecutionRequest.model_validate(document)
        self.factory = lambda repo: SimpleNamespace(
            formal_sources=FormalMethods(repo, source_licenses=(license,))
        )
        self.conductor = KnowbotTestConductor(
            workbench_stores(db), formal_capabilities=self.factory
        )
        route = partial(
            InstructionRequests.for_account,
            routes=(ROUTE,),
            model=ROUTE.model,
            prices=PRICES,
            default_url=ROUTE.base_url,
        )
        async with db() as session:
            repo = WorkbenchRepository(session)
            svc = FormalExecutions(repo, self.factory(repo))
            with pytest.raises(CapabilityError, match="disabled"):
                await svc.accept(self.request, caller="knowlab")
            svc.instruction_service = route
            document["execution"]["budget"]["limit"] = "0.01"
            with pytest.raises(CapabilityError, match="budget"):
                await svc.accept(
                    FormalMethodExecutionRequest.model_validate(document),
                    caller="knowlab",
                )
            assert await repo.get_test_run(str(self.request.run_id)) is None
            result = await svc.accept(self.request, caller="knowlab")
            assert result["status"] == "pending"
            assert await svc.accept(self.request, caller="knowlab") == result
        await self.conductor.advance(str(self.request.run_id))
        return (await status(db, self.request, self.factory))["cases"][0]["task_id"]

    async def finish(self, db):
        await settle(self.conductor, self.request)
        result = await status(db, self.request, self.factory)
        if self.expected != "ready":
            assert (
                result["status"] == "failed" and result["admission"]["receipt"] is None
            )
            assert result["cases"][0]["assertions"][0]["passed"] is False
            return
        assert result["status"] == "passed", result
        receipt = result["admission"]["receipt"]
        version = FrozenFormalMethod.model_validate(
            {
                "contract": "knowlab/frozen-formal-method/v1",
                "owner_subject": "author",
                "visibility": "private",
                "reference": {
                    "method_id": self.request.method_id,
                    "version": 1,
                    "digest": self.request.draft.definition.digest(),
                },
                "draft": self.request.draft,
                "validation": receipt,
            }
        )
        async with db() as session:
            repo = WorkbenchRepository(session)
            proof = (await repo.formal_validation(OWNER, str(self.request.run_id)))[
                "execution_evidence"
            ]
            assert proof["adapter"] == "knowbench/formal-instruction-host/v1"
            evidence = proof["cases"][0]["governance"]
            assert evidence["usage"]["turn_id"] == evidence["request"]["turn_id"]
            assert evidence["ledger_id"] and evidence["quote"]["model"] == ROUTE.model
            assert await self.factory(repo).formal_sources.register(
                version, caller="knowlab"
            )
            from app.application.workbench.tool_registry import (
                formal_registration,
                installed_registration,
            )

            method = await formal_registration(repo, OWNER, version.reference)
            assert (method.method_kind, method.runner_route) == (
                "tool",
                "formal_instruction",
            )
            assert "host" in method.allowed_callers
            asset = await installed_registration(
                repo, OWNER, str(method.source_asset_id)
            )
            assert method.path == "formal" and method.source_kind == "skill_instruction"
            assert (
                asset.path == "installed" and asset.revision == method.source_revision
            )
            assert method.source_entry == "SKILL.md"
            assert any(
                e.entry == method.source_entry and e.tool_key is None
                for e in asset.entries
            )
            formal = self.factory(repo).formal_sources
            catalog = FormalMethodCatalog(repo, formal, formal.operations)
            assert (
                await catalog.resolve_owned_admission(
                    OWNER, version.reference, resolved={}
                )
                == version
            )
            # The author qualification confers no tool-dispatch or third-party entitlement.
            with pytest.raises((ValueError, CapabilityError)):
                await catalog.resolve_owned(OWNER, version.reference, caller="host")
            wrong = version.reference.model_copy(
                update={"digest": "sha256:" + "0" * 64}
            )
            with pytest.raises(CapabilityError):
                await catalog.resolve_owned_admission(OWNER, wrong, resolved={})
            formal.source_licenses = ()
            with pytest.raises(CapabilityError):
                await catalog.resolve_owned_admission(
                    OWNER, version.reference, resolved={}
                )

        return version


@pytest.mark.parametrize("expected", ["ready", "different"])
async def test_author_instruction_suite(db, expected):
    trial = AuthorTrial(expected)
    s = await stack(db, author_trial=trial.start)
    s.units.ready = True
    await drive(s)
    await trial.finish(db)


async def test_author_cancels_before_dispatch(db):
    trial = AuthorTrial()
    s = await stack(db, author_trial=trial.start)
    async with s.host() as svc:
        result = await FormalExecutions(svc.repo, trial.factory(svc.repo)).cancel(
            "author", str(trial.request.run_id), caller="knowlab"
        )
        assert (
            result["status"] == "cancelled" and result["admission"]["receipt"] is None
        )
        assert not await svc.repo.list_attempts(s.task)


async def test_tariff_increase_cannot_expand_author_budget(db):
    trial = AuthorTrial()
    s = await stack(db, author_trial=trial.start)
    s.driver.routes = (replace(ROUTE, context_tokens=1_000_000),)
    s.units.ready = True
    await drive(s)
    await settle(trial.conductor, trial.request)
    result = await status(db, trial.request, trial.factory)
    assert result["status"] == "failed" and result["admission"]["receipt"] is None
    assert not s.units.bindings and not s.turns


async def test_output_without_matching_settlement_cannot_freeze(db):
    trial = AuthorTrial()
    s = await stack(db, author_trial=trial.start)
    s.units.ready = True
    await drive(s)
    async with s.host() as svc, svc.repo.transaction():
        (attempt,) = await svc.repo.list_attempts(s.task)
        refs = attempt["refs"]
        await svc.repo.update_attempt(
            str(attempt["id"]),
            refs={
                **refs,
                "instruction_usage": {**refs["instruction_usage"], "amount": "0"},
            },
        )
    await settle(trial.conductor, trial.request)
    result = await status(db, trial.request, trial.factory)
    assert result["cases"][0]["assertions"][0]["passed"]
    assert result["status"] == "failed" and result["admission"]["receipt"] is None


async def test_private_instruction_remains_qualified_only_after_safe_cleanup(db):
    trial = AuthorTrial()
    s = await stack(db, author_trial=trial.start)
    s.units.ready = True
    await drive(s)
    version = await trial.finish(db)
    run_id = str(trial.request.run_id)
    async with s.host() as svc:
        (attempt,) = await svc.repo.list_attempts(s.task)
        with pytest.raises(CleanupPending, match="Instruction usage"):
            async with svc.repo.transaction():
                await svc.repo.purge_test_run(run_id)
    for _ in range(2):
        await s.driver.cleanup(OWNER, s.task, str(attempt["id"]))
    async with s.host() as svc:
        async with svc.repo.transaction():
            await svc.repo.purge_test_run(run_id)
        catalog = FormalMethodCatalog(svc.repo, svc.formal, svc.formal.operations)
        assert (
            await catalog.resolve_owned_admission(OWNER, version.reference, resolved={})
            == version
        )


@pytest.mark.parametrize(
    "decision",
    [
        "complete",
        "decline_second",
        "revoke_parent",
        "cancel_approved",
        "cancel_claimed",
    ],
)
async def test_fixed_instruction_plan_approves_and_hands_off_exact_artifact(
    db, decision
):
    await fixed_instruction_plan(db, decision)


async def fixed_instruction_plan(db, decision):
    import json
    from decimal import Decimal

    from formal_plan_support import plan_request
    from test_capabilities_db import Remote
    from test_formal_execution_db import approve

    from app.application.workbench.capabilities import Capabilities
    from app.application.workbench.formal_execution import FormalExecution

    trial = AuthorTrial()
    s = await stack(db, author_trial=trial.start)
    s.units.ready = True
    await drive(s)
    leaf = await trial.finish(db)
    route = partial(
        InstructionRequests.for_account,
        routes=(ROUTE,),
        model=ROUTE.model,
        prices=PRICES,
        default_url=ROUTE.base_url,
    )

    def factory(repo):
        caps = Capabilities(repo, Remote(), instruction_service=route)
        caps.formal_sources = trial.factory(repo).formal_sources
        return caps

    conductor = KnowbotTestConductor(workbench_stores(db), formal_capabilities=factory)
    graph = FormalExecution(workbench_stores(db), factory, instructions=s.driver)
    case = trial.request.execution.suite.cases[0].model_dump(mode="json")
    case["input"]["values"] = {"status": "awaiting review"}
    request = plan_request(
        trial.request,
        leaf,
        case,
        input_name="status",
        output_name="summary",
    )
    async with s.host() as svc:
        await FormalExecutions(svc.repo, factory(svc.repo)).accept(
            request, caller="knowlab"
        )
    await conductor.advance(str(request.run_id))
    result = await status(db, request, factory)
    task = result["cases"][0]["task_id"]
    s.units.task = task
    s.wire["client_metadata"] = {"session_note": "private-session-marker"}
    artifacts = []
    for index in range(2):
        (action,) = result["actions"]
        subject = action["subject"]
        assert subject["arguments"] == {
            "status": "ready" if index else "awaiting review"
        }
        assert subject["data_transfer"]["destination"] == ROUTE.base_url + "/responses"
        if index:
            first = artifacts[0]
            assert subject["artifact_read"]["fields"]["status"] == {
                "producer_path": ["first"],
                "artifact_id": first["id"],
                "name": first["name"],
                "version": first["version"],
                "digest": first["digest"],
                "output": "summary",
            }
        assert await graph.drive(task) == "WAITING"
        assert len(s.turns) == index + 1
        if index and decision == "decline_second":
            await approve(db, request, factory, result, decision="decline")
            assert await graph.drive(task) == "FAILED"
            assert len(s.turns) == 2
            async with s.host() as svc:
                assert (
                    Decimal((await svc.repo.get_task(task))["budget"]["reserved"]) == 0
                )
            return
        await approve(db, request, factory, result)
        if index and decision == "cancel_approved":
            async with s.host() as svc:
                await FormalExecutions(svc.repo, factory(svc.repo)).cancel(
                    "author", str(request.run_id), caller="knowlab"
                )
                call = await svc.repo.capability_call(OWNER, action["call_id"])
                assert call["status"] == "failed" and call["detail"]["result"] == {
                    "error": "cancelled_before_model_request"
                }
                assert (await svc.repo.get_task(task))["state"] == "CANCELLED"
                assert (
                    Decimal((await svc.repo.get_task(task))["budget"]["reserved"]) == 0
                )
                assert len(s.turns) == 2
            return
        if index and decision == "revoke_parent":
            async with s.host() as svc, svc.repo.transaction():
                parent = subject["artifact_read"]["path"][0]["call_id"]
                auth = await svc.repo.method_authorization(OWNER, parent, lock=True)
                version = auth["revision"]
                await svc.repo.save_method_authorization(
                    OWNER,
                    str((await svc.repo.get_task(task))["session_id"]),
                    {**auth, "revoked": True, "revision": version + 1},
                    previous=version,
                )
            await graph.drive(task)
            assert await graph.drive(task) == "FAILED"
            assert len(s.turns) == 2
            return
        async with s.host() as svc, svc.repo.transaction():
            await svc.authority(OWNER, await svc.repo.get_task(task), "knowlab")
        if index and decision == "cancel_claimed":

            async def cancel_sent_request():
                async with s.host() as svc:
                    await FormalExecutions(svc.repo, factory(svc.repo)).cancel(
                        "author", str(request.run_id), caller="knowlab"
                    )

            s.units.after_claim = cancel_sent_request
        message = s.wire["input"][0]["content"][0]
        job = json.loads(message["text"])
        job["arguments"] = subject["arguments"]
        message["text"] = json.dumps(job)
        s.units.uid, s.units.worker = str(uuid4()), str(uuid4())
        await graph.drive(task)
        await graph.drive(task)
        async with s.host() as svc:
            attempt = (await svc.repo.list_attempts(task))[index]
            if index and decision == "cancel_claimed":
                call = await svc.repo.capability_call(OWNER, action["call_id"])
                assert (
                    call["status"] == "unknown"
                    and call["detail"]["result"]["error"]
                    == "cancelled_after_model_request"
                )
                root = await svc.repo.get_task(task)
                assert root["state"] == "CANCELLED" and not Decimal(
                    root["budget"]["reserved"]
                )
                charges = [
                    row
                    for row in await svc.repo.ledger_list(task)
                    if row["entry"] == "consume"
                ]
                assert (
                    len(charges)
                    == len({str(row["attempt_id"]) for row in charges})
                    == 2
                )
                assert sum(row["amount"] for row in charges) == Decimal(
                    root["budget"]["used"]
                )
                assert (
                    attempt["status"] == "CANCELLED" and not attempt["output_artifacts"]
                )
                return
            assert attempt["status"] == "COMPLETED", (
                attempt["refs"],
                (await svc.repo.get_task(task))["state"],
            )
            (artifact,) = attempt["output_artifacts"]
            artifacts.append(artifact)
            if index == 0:
                async with svc.repo.transaction():
                    newer = await svc.repo.put_artifact(
                        task_id=task,
                        attempt_id=None,
                        name=artifact["name"],
                        kind="step",
                        content={"summary": "unaccepted revision"},
                    )
                assert newer["version"] > artifact["version"]
            assert artifact["name"] == "formal-step:" + ("second" if index else "first")
        outgoing = s.provider_requests[-1]
        assert "private-session-marker" not in json.dumps(outgoing)
        assert (
            json.loads(outgoing["input"][-1]["content"][0]["text"])["arguments"]
            == subject["arguments"]
        )
        assert [m["role"] for m in outgoing["input"]] == ["developer", "user"]
        assert outgoing["store"] is False
        result = await status(db, request, factory)
    async with s.host() as svc:
        root = await svc.repo.get_task(task)
        assert root["state"] == "SUCCEEDED" and not Decimal(root["budget"]["reserved"])
        charges = [
            r for r in await svc.repo.ledger_list(task) if r["entry"] == "consume"
        ]
        assert len({str(r["attempt_id"]) for r in charges}) == 2
        assert sum(r["amount"] for r in charges) == Decimal(root["budget"]["used"])

    await settle(conductor, request)
    result = await status(db, request, factory)
    async with s.host() as svc:
        row = await svc.repo.formal_validation(OWNER, str(request.run_id))
        assert result["status"] == "passed", row["execution_evidence"]["cases"][0][
            "errors"
        ]
        from formal_plan_support import freeze

        version = freeze(request, result)
        caps = factory(svc.repo)
        await caps.formal_sources.register(version, caller="knowlab")
        catalog = FormalMethodCatalog(svc.repo, caps.formal_sources, caps.operations)
        assert (
            await catalog.resolve_owned_admission(
                OWNER,
                version.reference,
                resolved={(leaf.reference.method_id, leaf.reference.version): leaf},
            )
            == version
        )

        from copy import deepcopy

        from app.application.workbench.formal_orchestration_admission import (
            verify_sealed_plan,
        )

        altered = deepcopy(row["execution_evidence"])
        observed = altered["cases"][0]["steps"][1]["governance"]["artifact_read"][
            "binding"
        ]["fields"]["status"]
        observed["version"] += 1
        with pytest.raises(CapabilityError, match="input versions"):
            verify_sealed_plan(
                OWNER,
                version,
                request,
                altered,
                {(leaf.reference.method_id, leaf.reference.version): leaf},
                operations=caps.operations,
            )

    return s, factory, conductor, leaf, version, request
