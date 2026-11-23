"""Model admission boundaries over actual WS/HTTP, approvals and persisted calls."""

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from formal_model_support import ModelServer, setup
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_capabilities_db import OWNER, approval
from test_formal_model_execution_db import drive_to_approval
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_task_runtime_db import close

from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_test_runs import FormalExecutions
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import Forbidden
from app.domain.workbench.formal_method_admission_generated import FrozenFormalMethod
from app.domain.workbench.formal_method_execution_generated import (
    FormalMethodExecutionRequest,
    FormalTestSuiteDraft,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


async def test_model_admission_rejects_changed_evidence_and_never_opens_host_directory(
    db,
):
    fake, runner = ModelServer(), None
    await fake.start()
    try:
        request, remote, factory, conductor, runner, _, call = await drive_to_approval(
            db, fake
        )
        assert call is not None, fake.failures
        for _ in range(3):
            await conductor.advance(str(request.run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            caps = factory(repo)
            service = FormalExecutions(repo, caps)
            report = await service.status(
                "author", str(request.run_id), caller="knowlab"
            )
            assert report["status"] == "passed", report
            frozen = FrozenFormalMethod.model_validate(
                {
                    "contract": "knowlab/frozen-formal-method/v1",
                    "owner_subject": request.owner_subject,
                    "reference": {
                        "method_id": request.method_id,
                        "version": 1,
                        "digest": request.draft.definition.digest(),
                    },
                    "draft": request.draft,
                    "validation": report["admission"]["receipt"],
                    "visibility": "private",
                }
            )
            proof = (await repo.formal_validation(OWNER, str(request.run_id)))[
                "execution_evidence"
            ]
            changed = deepcopy(proof)
            changed["cases"][0]["model_provenance"]["cost"] = "0"
            query = text(
                "UPDATE workbench_formal_validations SET execution_evidence=cast(:proof as jsonb) WHERE run_id=:id"
            )
            async with repo.transaction():
                await session.execute(
                    query, {"id": str(request.run_id), "proof": json.dumps(changed)}
                )
            with pytest.raises(Forbidden, match="evidence"):
                await service.formal.register(frozen, caller="knowlab")
            assert await repo.formal_method(OWNER, str(request.method_id), 1) is None
            async with repo.transaction():
                await session.execute(
                    query, {"id": str(request.run_id), "proof": json.dumps(proof)}
                )
            registered = await service.formal.register(frozen, caller="knowlab")
            assert registered["status"] == "registered"
            catalog = FormalMethodCatalog(repo, caps.formal_sources, caps.operations)
            with pytest.raises(CapabilityError):
                await catalog.resolve_owned(OWNER, frozen.reference)
            items = await catalog.list_owned(OWNER)
            assert len(items) == 1 and not items[0]["available"] and items[0]["issue"]
            async with repo.transaction():
                await repo.purge_test_run(str(request.run_id))
            assert await service.formal.register(frozen, caller="knowlab") == registered
            assert (await repo.formal_validation(OWNER, str(request.run_id)))[
                "execution_evidence"
            ] == proof
            assert not (await catalog.list_owned(OWNER))[0]["available"]
        assert len(fake.turn_inputs) == len(remote.calls) == 1
    finally:
        if runner:
            await close(runner)
        await fake.close()


async def _decide_case(db, fake, request, case, runner, conductor, *, decision):
    await conductor.advance(str(request.run_id))
    if case.caller == "model":
        await runner.run_once()
    call = None
    for _ in range(300):
        async with db() as session:
            repo = WorkbenchRepository(session)
            row = next(
                r
                for r in await repo.list_test_cases(str(request.run_id))
                if r["case_id"] == case.case_id + ":1"
            )
            assert row["task_id"] is not None
            attempts = await repo.list_attempts(str(row["task_id"]))
            linked = (
                (attempts[-1].get("refs") or {}).get("capability_call_id")
                if attempts
                else None
            )
            if linked:
                call = await repo.capability_call(OWNER, linked)
                break
            task = await repo.get_task(str(row["task_id"]))
            if task["state"] in ("FAILED", "CANCELLED", "REJECTED"):
                break
        await asyncio.sleep(0.02)
    assert call is not None, fake.failures
    async with AsyncClient(
        transport=ASGITransport(fake.app), base_url="http://testserver"
    ) as http:
        response = await http.post(
            f"/api/workbench/capability-calls/{call['id']}/decision",
            json=approval(call, decision=decision).model_dump(),
        )
    assert response.status_code == 200, response.text
    if case.caller == "model":
        await runner.wait_drivers(timeout=30)
    else:
        # Host resumes synchronously through the conductor, without a model turn.
        await conductor.advance(str(request.run_id))
    await conductor.advance(str(request.run_id))


async def test_real_model_approval_negative_pass_does_not_replace_positive_coverage(db):
    fake, runner = ModelServer(), None
    await fake.start()
    try:
        original, remote, factory, conductor, runner = await setup(db, fake)
        # Submit a separate immutable request, never rewrite accepted test contents.
        doc = original.model_dump(mode="json", by_alias=True)
        positive = doc["execution"]["suite"]["cases"][0]
        negative = deepcopy(positive)
        negative.update(
            case_id="refusal",
            expected={
                "outcome": "rejected",
                "rejection_code": "approval_denied",
                "assertions": [],
            },
        )
        suite = FormalTestSuiteDraft.model_validate(
            {"suite_id": str(uuid4()), "version": 1, "cases": [negative, positive]}
        ).freeze()
        doc["run_id"] = str(uuid4())
        doc["execution"]["suite"] = suite.model_dump(mode="json")
        request = FormalMethodExecutionRequest.model_validate(doc)
        async with db() as session:
            repo = WorkbenchRepository(session)
            await FormalExecutions(repo, factory(repo)).accept(
                request, caller="knowlab"
            )
        for case in request.execution.suite.cases:
            await _decide_case(
                db, fake, request, case, runner, conductor, decision="decline"
            )
        await conductor.advance(str(request.run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            result = await FormalExecutions(repo, factory(repo)).status(
                "author", str(request.run_id), caller="knowlab"
            )
            assert result["status"] == "failed", result
            assert result["admission"]["receipt"] is None
            cases = {case["case_id"]: case for case in result["cases"]}
            assert cases["refusal"]["status"] == "passed"
            assert cases["refusal"]["outcome"] == "rejected"
            assert cases[positive["case_id"]]["status"] == "failed"
            proof = (await repo.formal_validation(OWNER, str(request.run_id)))[
                "execution_evidence"
            ]
            assert all(case["validated"] for case in proof["cases"]), proof["cases"]
        assert len(fake.turn_inputs) == 2 and not remote.calls
    finally:
        if runner:
            await close(runner)
        await fake.close()


async def test_mixed_host_and_model_success_seals_both_callers_and_registers(db):
    fake, runner = ModelServer(), None
    await fake.start()
    try:
        original, remote, factory, conductor, runner = await setup(db, fake)
        doc = original.model_dump(mode="json", by_alias=True)
        model_case = doc["execution"]["suite"]["cases"][0]
        host_case = deepcopy(model_case)
        host_case.update(case_id="host-notice", caller="host")
        host_case["input"].pop("model_prompt")
        suite = FormalTestSuiteDraft.model_validate(
            {
                "suite_id": str(uuid4()),
                "version": 1,
                "cases": [host_case, model_case],
            }
        ).freeze()
        doc["run_id"] = str(uuid4())
        doc["draft"]["allowed_callers"] = ["host", "model"]
        doc["execution"]["suite"] = suite.model_dump(mode="json")
        request = FormalMethodExecutionRequest.model_validate(doc)
        async with db() as session:
            repo = WorkbenchRepository(session)
            await FormalExecutions(repo, factory(repo)).accept(
                request, caller="knowlab"
            )
        for case in request.execution.suite.cases:
            await _decide_case(
                db, fake, request, case, runner, conductor, decision="accept"
            )
        await conductor.advance(str(request.run_id))
        async with db() as session:
            repo = WorkbenchRepository(session)
            service = FormalExecutions(repo, factory(repo))
            report = await service.status(
                "author", str(request.run_id), caller="knowlab"
            )
            assert report["status"] == "passed", report
            assert len(report["cases"]) == 2
            assert all(
                case["status"] == "passed" and case["outcome"] == "succeeded"
                for case in report["cases"]
            )
            receipt = report["admission"]["receipt"]
            assert receipt["execution"]["validated_callers"] == ["host", "model"]
            proof = (await repo.formal_validation(OWNER, str(request.run_id)))[
                "execution_evidence"
            ]
            assert len(proof["cases"]) == 2
            assert all(case["validated"] for case in proof["cases"])
            assert proof["cases"][0]["model_turn_ids"] == []
            assert len(proof["cases"][1]["model_turn_ids"]) == 1
            assert proof["cases"][1]["model_provenance"]["validated"]
            frozen = FrozenFormalMethod.model_validate(
                {
                    "contract": "knowlab/frozen-formal-method/v1",
                    "owner_subject": request.owner_subject,
                    "reference": {
                        "method_id": request.method_id,
                        "version": 1,
                        "digest": request.draft.definition.digest(),
                    },
                    "draft": request.draft,
                    "validation": receipt,
                    "visibility": "private",
                }
            )
            registered = await service.formal.register(frozen, caller="knowlab")
            assert registered["status"] == "registered"
        assert len(fake.turn_inputs) == 1 and len(remote.calls) == 2
        assert not fake.failures
    finally:
        if runner:
            await close(runner)
        await fake.close()
