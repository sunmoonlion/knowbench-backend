"""Nested instruction egress, resumed settlement, and retained method qualification."""

import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from formal_plan_support import freeze
from formal_recursive_support import parent_request
from sqlalchemy import text
from test_formal_execution_db import approve, settle, status
from test_formal_instruction_trial_db import fixed_instruction_plan
from test_instruction_requests_db import PRICES, ROUTE
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.formal_execution import FormalExecution
from app.application.workbench.formal_method_catalog import FormalMethodCatalog
from app.application.workbench.formal_orchestration_admission import verify_sealed_plan
from app.application.workbench.formal_test_runs import FormalExecutions
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.errors import IdempotencyConflict
from app.domain.workbench.formal_method_execution_generated import FormalTestSuiteDraft


@pytest.mark.parametrize(
    "scenario",
    [
        "registered",
        "complete",
        "intermediate_revoked",
        "registration_changed",
        "budget",
    ],
)
async def test_nested_instruction_graph(db, scenario):
    s, factory, conductor, leaf, child, base = await fixed_instruction_plan(
        db, "complete", registered=scenario == "registered"
    )
    req = parent_request(base, [child, child], depth=3, expansions=7, steps=6)
    suite = req.execution.suite.model_dump(mode="json", exclude={"digest"})
    suite["cases"][0]["expected"]["assertions"][0]["value"] = "ready"
    req = req.model_copy(
        update={
            "execution": req.execution.model_copy(
                update={"suite": FormalTestSuiteDraft.model_validate(suite).freeze()}
            )
        }
    )
    if scenario == "budget":
        budget = req.execution.budget.model_copy(
            update={"limit": str(Decimal(ROUTE.quote(PRICES)["maximum"]).normalize())}
        )
        req = req.model_copy(
            update={"execution": req.execution.model_copy(update={"budget": budget})}
        )
    async with s.host() as svc:
        await FormalExecutions(svc.repo, factory(svc.repo)).accept(
            req, caller="knowlab"
        )
    await conductor.advance(str(req.run_id))
    run = await status(db, req, factory)
    task = run["cases"][0]["task_id"]
    s.units.task = task
    initial = len(s.provider_requests)
    for i in range(4):
        (action,) = run["actions"]
        subject = action["subject"]
        binding = subject["artifact_read"]
        assert len(binding["path"]) == 3
        # The second child consumes the first child's accepted export.
        if i == 2:
            assert binding["fields"]["status"]["producer_path"] == ["first"]
        await approve(db, req, factory, run)
        async with s.host() as svc:
            current = await svc.repo.get_task(task)
            attempt = str(current["active_attempt_id"])
            if scenario == "intermediate_revoked" and i == 1:
                async with svc.repo.transaction():
                    parent_id = binding["path"][1]["call_id"]
                    auth = await svc.repo.method_authorization(
                        OWNER, parent_id, lock=True
                    )
                    await svc.repo.save_method_authorization(
                        OWNER,
                        str(current["session_id"]),
                        {**auth, "revoked": True, "revision": auth["revision"] + 1},
                        previous=auth["revision"],
                    )
            if scenario == "registration_changed" and i == 1:
                async with db() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE workbench_formal_methods SET digest=:digest WHERE method_id=:id"
                        ),
                        {
                            "id": child.reference.method_id,
                            "digest": "sha256:" + "0" * 64,
                        },
                    )
            denied = i == 1 and scenario in (
                "intermediate_revoked",
                "registration_changed",
            )
            if denied:
                reason = (
                    "ancestor execution scope"
                    if scenario == "intermediate_revoked"
                    else "accessible registration"
                )
                with pytest.raises(CapabilityError, match=reason):
                    await svc.claim(
                        OWNER,
                        task,
                        attempt,
                        caller="knowlab",
                        credential_id=s.cred["id"],
                        turn_id="turn-a",
                        body=s.wire,
                    )
        driver = FormalExecution(workbench_stores(db), factory, instructions=s.driver)
        if denied:
            await driver.drive(task)
            assert await driver.drive(task) == "FAILED"
            assert len(s.provider_requests) == initial + 1
            async with s.host() as svc:
                calls = await svc.repo.list_task_calls(OWNER, task)
                assert all(c["status"] in ("failed", "succeeded") for c in calls)
            return
        s.units.uid, s.units.worker = str(uuid4()), str(uuid4())
        message = s.wire["input"][0]["content"][0]
        job = json.loads(message["text"])
        job["arguments"] = subject["arguments"]
        message["text"] = json.dumps(job)
        await s.driver.drive(task, leaf_attempt_id=attempt)
        if i == 0:
            async with s.host() as svc:
                with pytest.raises(IdempotencyConflict, match="already claimed"):
                    await svc.claim(
                        OWNER,
                        task,
                        attempt,
                        caller="knowlab",
                        credential_id=s.cred["id"],
                        turn_id="turn-a",
                        body=s.wire,
                    )
        # The provider replied; a fresh graph driver resumes before the method return.
        await driver.drive(task)
        assert len(s.provider_requests) == initial + i + 1
        if scenario == "budget":
            async with s.host() as svc:
                current = await svc.repo.get_task(task)
                assert current["state"] == "FAILED" and not Decimal(
                    current["budget"]["reserved"]
                )
                calls = await svc.repo.list_task_calls(OWNER, task)
                assert all(c["status"] in ("failed", "succeeded") for c in calls)
                assert any(
                    "Insufficient remaining" in c["detail"].get("error", "")
                    for c in calls
                )
                charges = [
                    c
                    for c in await svc.repo.ledger_list(task)
                    if c["entry"] == "consume"
                ]
                assert (
                    len(charges) == 1
                    and charges[0]["amount"] == Decimal(current["budget"]["used"]) > 0
                )
            return
        run = await status(db, req, factory)
    await settle(conductor, req)
    run = await status(db, req, factory)
    async with s.host() as svc:
        row = await svc.repo.formal_validation(OWNER, str(req.run_id))
        evidence = row["execution_evidence"]
        assert run["status"] == "passed", evidence["cases"][0]["errors"]
        tree = evidence["cases"][0]
        assert len(tree["steps"]) == 4 and len(tree["nodes"]) == 6
        assert (
            Decimal(tree["task"]["budget"]["used"])
            == Decimal(evidence["model_cost"])
            > 0
        )
        version = freeze(req, run)
        caps = factory(svc.repo)
        await caps.formal_sources.register(version, caller="knowlab")
        deps = {(v.reference.method_id, v.reference.version): v for v in (leaf, child)}
        assert (
            await FormalMethodCatalog(
                svc.repo, caps.formal_sources, caps.operations
            ).resolve_owned_admission(OWNER, version.reference, resolved=deps)
            == version
        )
        for target, reason in [
            ("tree", "tree definition"),
            ("node", "invocation binding"),
        ]:
            historical = deepcopy(evidence)
            sample = historical["cases"][0]
            if target == "tree":
                sample["tree"]["method_kind"] = "expert_team"
            else:
                next(n for n in sample["nodes"] if n["kind"] == "expert")["kind"] = (
                    "expert_team"
                )
            with pytest.raises(CapabilityError, match=reason):
                verify_sealed_plan(
                    OWNER, version, req, historical, deps, operations=caps.operations
                )
        bad = deepcopy(evidence)
        case = bad["cases"][0]
        node = next(n for n in case["nodes"] if n["path"] == ["second", "first"])
        step = case["steps"][node["step_index"]]
        assert step["path"] == node["path"]
        step["governance"]["artifact_read"]["binding"]["fields"]["status"]["version"] = 42
        with pytest.raises(CapabilityError, match="input versions"):
            verify_sealed_plan(
                OWNER, version, req, bad, deps, operations=caps.operations
            )
