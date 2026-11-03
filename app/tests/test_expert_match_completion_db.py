"""A model reply is not a successful match until its directory evidence passes."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest
from test_capability_find_execution_db import Directory
from test_expert_match import QUESTION, directory, result
from test_find_expert_db import _project
from test_knowbot_test_runs_db import runner_for, setup_test_computer
from test_workbench_advisor_db import ScriptedAppServer, close, drive
from test_workbench_ledger_db import OWNER
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.find_expert import (
    FindExpertRefused,
    FindExpertService,
    find_expert,
    match_receipt,
)
from app.infrastructure.workbench.repository import WorkbenchRepository


def examples(damage):
    entries, good = directory(), result()
    if damage == "prose_capability":
        entries["experts"][0].update(
            declaration="scope/v1",
            runtime_contract="knowbench/fixed/v1",
            public_capabilities=[],
            scope={
                "does": "【F4】核对并复核文档，交付报告",
                "does_not": "",
                "elsewhere": "",
            },
        )
        for row in good["picks"][0]["coverage"]:
            row["capability_id"] = None
    bad = deepcopy(good)
    if damage == "prose_capability":
        bad["picks"][0]["coverage"][0]["capability_id"] = "F4"
    else:
        bad["requirements"][0]["question_quote"] = "核对……报告"
    return entries, good, bad


@pytest.mark.parametrize("damage", ["prose_capability", "joined_question_quote"])
@pytest.mark.parametrize("corrected", [True, False])
async def test_evidence_failure_reworks_with_reason_before_success(
    db, damage, corrected
):
    entries, good, bad = examples(damage)
    fake = ScriptedAppServer([json.dumps(bad), json.dumps(good if corrected else bad)])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)
        port = Directory(entries)
        async with db() as s:
            repo = WorkbenchRepository(s)
            made = await FindExpertService(repo, directory=port).start(
                owner_actor_id=OWNER,
                project_id=project,
                question=QUESTION,
                idempotency_key="match-validation",
                match_contract="knowbench/expert-match/v2",
            )
            await find_expert(repo, port, await repo.get_task(made["task_id"]))
        await drive(runner)
        assert len(fake.turn_inputs) == 2
        reason = (
            "coverage names an unknown public capability"
            if damage == "prose_capability"
            else "requirement quote is not in the original question"
        )
        assert reason in fake.turn_inputs[1]
        async with db() as s:
            repo = WorkbenchRepository(s)
            task = await repo.get_task(made["task_id"])
            attempts = await repo.list_attempts(made["task_id"])
            assert attempts[0]["status"] != "COMPLETED"
            assert len(attempts) == 2
            assert Decimal(task["budget"]["used"]) > 0
            assert Decimal(task["budget"]["limit"]) == 2
            found = await FindExpertService(repo, directory=port).result(
                OWNER, made["task_id"]
            )
            if corrected:
                assert task["state"] == "SUCCEEDED"
                assert found["outcome"] == "summon"
                assert (
                    await repo.get_artifact(task_id=made["task_id"], name="find_expert")
                )["content"] == good
            else:
                assert task["state"] == "WAITING"
                assert "outcome" not in found
                assert (
                    await repo.get_artifact(task_id=made["task_id"], name="find_expert")
                    is None
                )
        await drive(runner)
        assert len(fake.turn_inputs) == 2  # No unbounded paid retries.
    finally:
        await close(runner)
        await fake.close()


async def test_historical_invalid_success_returns_explanation_but_cannot_delegate(db):
    entries, good, bad = examples("prose_capability")
    fake = ScriptedAppServer([json.dumps(good)])
    await fake.start()
    runner = runner_for(db)
    try:
        await setup_test_computer(db, fake)
        project = await _project(db)
        port = Directory(entries)
        async with db() as s:
            repo = WorkbenchRepository(s)
            made = await FindExpertService(repo, directory=port).start(
                owner_actor_id=OWNER,
                project_id=project,
                question=QUESTION,
                idempotency_key="historical-match",
                match_contract="knowbench/expert-match/v2",
            )
            await find_expert(repo, port, await repo.get_task(made["task_id"]))
        await drive(runner)
        async with db() as s:
            repo = WorkbenchRepository(s)
            # Synthetic old schema-only acceptance, in this test's isolated DB.
            async with repo.transaction():
                old = await repo.get_artifact(
                    task_id=made["task_id"], name="find_expert"
                )
                await repo.put_artifact(
                    task_id=made["task_id"],
                    attempt_id=str(old["attempt_id"]),
                    name="find_expert",
                    kind="step",
                    content=bad,
                )
            before = await repo.get_task(made["task_id"])
            svc = FindExpertService(repo, directory=port)
            first = await svc.result(OWNER, made["task_id"])
            assert first == await svc.result(OWNER, made["task_id"])
            assert first["state"] == "SUCCEEDED"  # History is not rewritten by GET.
            assert "未通过核验" in first["error"]
            assert "outcome" not in first and "picks" not in first
            assert await repo.get_task(made["task_id"]) == before
            with pytest.raises(FindExpertRefused):
                await match_receipt(
                    repo,
                    before,
                    await repo.get_artifact(
                        task_id=made["task_id"], name="find_expert"
                    ),
                )
        assert len(fake.turn_inputs) == 1
    finally:
        await close(runner)
        await fake.close()
