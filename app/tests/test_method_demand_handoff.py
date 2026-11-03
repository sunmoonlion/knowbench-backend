"""Persisted matching evidence crosses the provider's incubation contract."""

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import jsonschema
import pytest

from app.application.workbench.find_expert import FindExpertRefused, FindExpertService
from app.domain.workbench.expert_match import digest
from tests.test_expert_match import QUESTION, directory
from tests.test_method_demand_match import demand_result


def setup():
    doc = demand_result()
    doc.update(
        outcome="none", recommend={"action": "incubate", "reason": "需要复核方法"}
    )
    doc["picks"][0]["coverage"][0]["status"] = "uncovered"
    task = {
        "id": "task",
        "project_id": "project",
        "profile_id": "FIND_EXPERT",
        "profile_version": "3",
        "state": "SUCCEEDED",
        "original_input": {"input": {"text": QUESTION}},
    }
    entries = directory()
    repo = AsyncMock()
    repo.get_task.return_value = task
    repo.subject_of_actor.return_value = "author"
    repo.get_artifact.return_value = {"version": 2, "content": doc}
    repo.list_task_events.return_value = [
        {"payload": {**entries, "directory_digest": digest(entries)}}
    ]
    desk = AsyncMock()
    return FindExpertService(repo, incubations=desk), repo, desk


async def test_validated_requirements_proposals_and_gaps_cross_provider_schema():
    svc, repo, desk = setup()
    await svc.ask_incubation("actor", "task")
    payload = desk.submit.call_args.args[0]
    assert payload["question"] == QUESTION
    assert payload["method_demand"]["method_proposals"][0]["id"] == "compare"
    assert payload["method_demand"]["gaps"][0]["requirement_id"] == "r1"
    assert payload["method_demand"]["match_artifact_version"] == 2
    assert "method_sources" not in json.dumps(payload)
    validate_provider_payload(payload)


def validate_provider_payload(payload):
    parent = Path(__file__).resolve().parents[3]
    lock = json.loads(
        (parent / "contracts/knowlab-runtime-incubation-v2-lock.json").read_text()
    )
    schema = parent.parent / lock["provider"] / lock["schema"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    jsonschema.validate(payload, json.loads(schema.read_text()))


@pytest.mark.parametrize(
    "damage",
    ["running", "wrong_contract", "bad_quote", "summon", "missing", "not_object"],
)
async def test_invalid_match_never_becomes_an_incubation_request(damage):
    svc, repo, desk = setup()
    doc = repo.get_artifact.return_value["content"]
    if damage == "running":
        repo.get_task.return_value["state"] = "RUNNING"
    elif damage == "wrong_contract":
        doc["contract"] = "knowbench/expert-match/v2"
    elif damage == "bad_quote":
        doc["requirements"][0]["question_quote"] = "forged"
    elif damage == "summon":
        doc["outcome"] = "summon"
    elif damage == "missing":
        repo.get_artifact.return_value = None
    else:
        repo.get_artifact.return_value["content"] = []
    with pytest.raises(FindExpertRefused):
        await svc.ask_incubation("actor", "task")
    desk.submit.assert_not_called()
