"""Frozen dynamic catalog contract; deliberately not execution acceptance."""

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from knowbot_fixtures import KNOWBOT
from knowbot_fixtures import manifest as fixed_manifest

from app.domain.workbench.errors import KnowbotInvalid
from app.domain.workbench.knowbots import catalog_from_manifest, pack_from_manifest
from app.domain.workbench.method_catalog import render

PARENT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).parent / "fixtures/dynamic-methods-manifest.json"


def manifest():
    return json.loads(FIXTURE.read_text())


def parse(doc):
    return catalog_from_manifest(doc, knowbot_id=KNOWBOT, version="1")


def test_catalog_keeps_frozen_method_identity_without_a_workflow():
    doc = manifest()
    before = json.dumps(doc, sort_keys=True)
    catalog = parse(doc)
    assert catalog.planning.final_units == ("report",)
    assert catalog.method_units[1].selection.max_calls == 3
    assert catalog.method_units[0].contract.input_refs == ("inspect",)
    assert not hasattr(catalog, "workflow")
    assert not hasattr(catalog, "step")
    assert json.dumps(doc, sort_keys=True) == before


def test_incomplete_dynamic_runner_cannot_admit_a_catalog_as_fixed_steps():
    with pytest.raises(KnowbotInvalid, match="dynamic method execution is not enabled"):
        pack_from_manifest(manifest(), knowbot_id=KNOWBOT, version="1")
    assert pack_from_manifest(
        fixed_manifest(), knowbot_id=KNOWBOT, version="1"
    ).workflow
    old_label = manifest() | {"schema": "knowbot-manifest/v2"}
    with pytest.raises(KnowbotInvalid):
        pack_from_manifest(old_label, knowbot_id=KNOWBOT, version="1")
    with pytest.raises(KnowbotInvalid):
        parse(fixed_manifest())


@pytest.mark.parametrize(
    "changes",
    [
        {"runtime_contract": "knowbench/dynamic-methods/v2"},
        {"workflow": []},
        {"task_plan": {}},
        {"version": "2"},
        {"method_units": []},
    ],
)
def test_catalog_rejects_unknown_versions_or_runtime_state(changes):
    doc = manifest()
    doc["pack"].update(changes)
    with pytest.raises(KnowbotInvalid):
        parse(doc)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_steps": True},
        {"max_replans": -1},
        {"max_steps": 1},
        {"final_units": []},
        {"final_units": ["invented"]},
        {"final_units": ["report", "report"]},
        {"reserve": "0"},
        {"method_text": " "},
        {"max_replans": 11},
    ],
)
def test_planning_limits_and_required_outputs_are_hard_bounds(changes):
    doc = manifest()
    doc["pack"]["planning"].update(changes)
    with pytest.raises(KnowbotInvalid):
        parse(doc)


@pytest.mark.parametrize(
    "changes",
    [
        {"tools": ["run_sql"]},
        {"tools": ["read_input", "read_input"]},
        {"input_refs": ["invented"]},
        {"input_refs": ["report"]},
        {"input_refs": ["inspect", "inspect"]},
        {"input_refs": []},
        {"step_id": "__task_plan"},
        {"output_artifact": "other"},
        {"acceptance": []},
        {"output_schema": {}},
        {"mode": "compete"},
        {"tool_policy": {"command_allow": [["ls"]]}},
        {"tool_policy": {"command": "allow"}},
        {"tool_policy": {"network": "allow"}},
        {"output_file": "../outside"},
        {"reserve": "NaN"},
        {"max_reworks": -1},
    ],
)
def test_method_cannot_bypass_input_output_tool_and_execution_constraints(changes):
    doc = manifest()
    doc["pack"]["method_units"][0]["contract"].update(changes)
    with pytest.raises(KnowbotInvalid):
        parse(doc)


@pytest.mark.parametrize("case", ["duplicate", "cycle", "too_many", "wrong_binding"])
def test_cross_method_and_binding_integrity(case):
    doc = manifest()
    units = doc["pack"]["method_units"]
    if case == "duplicate":
        units.append(units[0])
    elif case == "cycle":
        units[1]["contract"]["input_refs"] = ["report"]
    elif case == "too_many":
        units[1]["selection"]["max_calls"] = 101
    else:
        doc["bindings"] = [
            {
                "material_id": "x",
                "version_no": 1,
                "sha256": "a" * 64,
                "basis_ref": f"expert:{KNOWBOT}@2",
            }
        ]
    with pytest.raises(KnowbotInvalid):
        parse(doc)


def test_provider_schema_is_generated_and_consumer_lock_matches():
    path = PARENT / "contracts/dynamic-methods/v1/catalog.schema.json"
    if not path.exists():
        pytest.skip("parent contract checkout required")
    assert path.read_text() == render()
    release = json.loads((path.parent / "contract-manifest.json").read_text())
    assert release["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    jsonschema.validate(manifest()["pack"], json.loads(path.read_text()))
    lock = json.loads(
        (PARENT / "contracts/knowmart-knowbot-manifest-v3-lock.json").read_text()
    )
    schema = PARENT.parent / lock["provider"] / lock["schema"]
    if not schema.exists():
        pytest.skip("knowmart provider checkout required")
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    jsonschema.validate(manifest(), json.loads(schema.read_text()))


def test_incubation_passes_dynamic_body_and_accepts_only_bounded_author_proposals():
    from pydantic import ValidationError

    from app.domain.workbench.incubations import Suggestion, Workdraft

    planning = manifest()["pack"]["planning"]
    raw = {"planning": planning, "steps": []}
    assert Workdraft(body=raw).body == raw
    suggestion = {
        "id": "p",
        "path": "planning",
        "op": "set",
        "value": planning,
        "reason": "author confirmation required",
    }
    Suggestion.model_validate(suggestion)
    Suggestion.model_validate(
        {
            **suggestion,
            "path": "steps/inspect/selection",
            "value": manifest()["pack"]["method_units"][1]["selection"],
        }
    )
    with pytest.raises(ValidationError):
        Suggestion.model_validate({**suggestion, "value": {**planning, "advanced": {}}})
    with pytest.raises(ValidationError):
        Suggestion.model_validate(
            {
                **suggestion,
                "path": "steps/inspect",
                "value": {"method_text": "replace frozen method"},
            }
        )
