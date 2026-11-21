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
