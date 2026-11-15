"""Independent evidence audit over real task/call/approval persistence."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_formal_execution_db import approve, setup, status
from test_workbench_ledger_db import db as db  # noqa: F401

from app.application.workbench.formal_evidence import collect_case_evidence
from app.infrastructure.workbench.repository import WorkbenchRepository


async def completed(db, *, cases=None, decision="accept"):
    request, _, _, factory, conductor = await setup(db, cases=cases)
    await conductor.advance(str(request.run_id))
    pending = await status(db, request, factory)
    if pending["actions"]:
        await approve(db, request, factory, pending, decision=decision)
    await conductor.advance(str(request.run_id))
    await conductor.advance(str(request.run_id))
    result = (await status(db, request, factory))["cases"][0]
    return request, result


async def collect(db, request, result):
    async with db() as session:
        return await collect_case_evidence(
            WorkbenchRepository(session),
            request,
            request.execution.suite.cases[0],
            1,
            result,
        )


async def test_real_case_keeps_sufficient_authority_without_secrets(db):
    request, result = await completed(db)
    evidence = await collect(db, request, result)
    assert evidence["validated"], evidence["errors"]
    assert evidence["governance"]["approved"]
    assert evidence["governance"]["direct_grants"]
    assert evidence["governance"]["authorization"]["grants"][0]["delegable"] is False
    assert evidence["model_turn_ids"] == []
    assert "approval_token" not in json.dumps(evidence)


@pytest.mark.parametrize("mutation", ["call", "task", "verdict", "assertion"])
async def test_case_identity_and_verdict_cannot_be_claimed(db, mutation):
    request, result = await completed(db)
    result = deepcopy(result)
    if mutation == "call":
        result["call_id"] = str(uuid4())
    elif mutation == "task":
        result["task_id"] = str(uuid4())
    elif mutation == "verdict":
        result["status"] = "failed"
    else:
        result["assertions"][0]["passed"] = False
    evidence = await collect(db, request, result)
    assert not evidence["validated"] and evidence["errors"]


async def test_adapter_output_is_recomputed_not_taken_from_attempt_claim(db):
    request, result = await completed(db)
    async with db() as session:
        await session.execute(
            text("""UPDATE workbench_calls
          SET detail=jsonb_set(detail, '{result,structuredContent,receipt}', '"changed"'::jsonb)
          WHERE id=:id"""),
            {"id": result["call_id"]},
        )
        await session.commit()
    evidence = await collect(db, request, result)
    assert not evidence["validated"]
    assert "adapter_output_mismatch" in evidence["errors"]


async def test_failed_business_assertion_does_not_discard_valid_governance(db):
    cases = [
        {
            "case_id": "notice",
            "input": {"values": {"to": "test@example.test"}},
            "repetitions": 1,
            "minimum_passes": 1,
            "expected": {
                "outcome": "succeeded",
                "assertions": [
                    {
                        "kind": "equals",
                        "pointer": "/receipt",
                        "value": "different answer",
                    },
                ],
            },
        }
    ]
    request, result = await completed(db, cases=cases)
    assert result["status"] == "failed" and result["outcome"] == "succeeded"
    evidence = await collect(db, request, result)
    assert evidence["validated"], evidence["errors"]


async def test_actual_approval_denial_is_safe_evidence_without_approval_grant(db):
    request, result = await completed(db, decision="decline")
    assert result["outcome"] == "rejected"
    evidence = await collect(db, request, result)
    assert evidence["validated"], evidence["errors"]
    assert evidence["governance"]["approved"] is False


async def test_expired_call_uses_real_expiry_when_driver_cancels_interaction(db):
    request, _, remote, factory, conductor = await setup(db)
    await conductor.advance(str(request.run_id))
    pending = await status(db, request, factory)
    action = pending["actions"][0]
    async with db() as session:
        await session.execute(
            text("""UPDATE workbench_interactions
          SET expires_at=now()-interval '1 second' WHERE id=:id"""),
            {"id": action["interaction_id"]},
        )
        await session.commit()
    await conductor.advance(str(request.run_id))
    await conductor.advance(str(request.run_id))
    result = (await status(db, request, factory))["cases"][0]
    assert result["outcome"] == "rejected" and not remote.calls
    evidence = await collect(db, request, result)
    assert evidence["validated"], evidence["errors"]
    assert evidence["governance"]["interaction_status"] == "cancelled"
    assert not evidence["governance"]["approved"]


async def test_invalid_input_is_recomputed_from_frozen_case_without_dispatch(db):
    positive = {
        "case_id": "positive",
        "input": {"values": {"to": "test@example.test"}},
        "repetitions": 1,
        "minimum_passes": 1,
        "expected": {
            "outcome": "succeeded",
            "assertions": [
                {
                    "kind": "equals",
                    "pointer": "/receipt",
                    "value": "expected-never-sent",
                },
            ],
        },
    }
    negative = {
        "case_id": "missing",
        "input": {"values": {}},
        "repetitions": 1,
        "minimum_passes": 1,
        "expected": {
            "outcome": "rejected",
            "rejection_code": "input_invalid",
            "assertions": [],
        },
    }
    request, result = await completed(db, cases=[negative, positive])
    assert result["status"] == "passed" and result["call_id"] is None
    evidence = await collect(db, request, result)
    assert evidence["validated"], evidence["errors"]
    assert evidence["governance"] is None
