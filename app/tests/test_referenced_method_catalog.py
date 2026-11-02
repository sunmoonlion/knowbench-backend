"""Read immutable producer snapshots, while keeping dynamic execution closed."""

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from method_plan_fixtures import proposal
from test_dynamic_method_catalog import parse

from app.domain.workbench.errors import KnowbotInvalid
from app.domain.workbench.knowbots import pack_from_manifest
from app.domain.workbench.method_catalog import ReferencedMethodCatalog, render
from app.domain.workbench.method_plan import validate_plan

PARENT = Path(__file__).resolve().parents[3]


def manifest():
    return json.loads(
        (
            Path(__file__).parent / "fixtures/referenced-methods-manifest.json"
        ).read_text()
    )


def test_pinned_method_snapshot_supports_planning_but_not_execution():
    doc = manifest()
    catalog = parse(doc)
    assert isinstance(catalog, ReferencedMethodCatalog)
    assert catalog.method_sources["report"].reference.version == 2
    assert catalog.method_units[0].contract.step_version == "2"
    validate_plan(
        catalog,
        proposal(),
        previous=None,
        protected_prefix=0,
        completed_prefix=0,
        results={},
        available_tools={"read_input"},
    )
    with pytest.raises(KnowbotInvalid, match="dynamic method execution is not enabled"):
        pack_from_manifest(doc, knowbot_id="synthetic", version="1")


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "digest",
        "definition",
        "slots",
        "limits",
        "condition",
        "schema",
        "alias",
        "v1",
        "compiler",
    ],
)
def test_reject_inconsistent_references_and_widened_selection(change):
    doc = manifest()
    pack = doc["pack"]
    source = pack["method_sources"]["report"]
    unit = pack["method_units"][0]
    if change == "version":
        source["reference"]["version"] = 3
    elif change == "digest":
        source["reference"]["digest"] = "sha256:" + "0" * 64
    elif change == "definition":
        source["definition"]["step"]["does"] = "替换原文"
    elif change == "slots":
        source["reference"]["inputs"] = {"records": "review"}
    elif change == "limits":
        unit["selection"]["max_calls"] = 2
    elif change == "condition":
        unit["selection"]["applies_when"] = "任何问题都做"
    elif change == "schema":
        pack["method_units"][1]["contract"]["output_schema"] = {
            "rows": {"type": "text"}
        }
    elif change == "alias":
        pack["method_sources"]["unknown"] = pack["method_sources"].pop("report")
    elif change == "compiler":
        pack["method_compiler"] = "unknown"
    else:
        pack["runtime_contract"] = "knowbench/dynamic-methods/v1"
    with pytest.raises(KnowbotInvalid):
        parse(doc)


def test_v2_provider_schema_and_author_definition_lock():
    path = PARENT / "contracts/dynamic-methods/v2/catalog.schema.json"
    assert path.read_text() == render(referenced=True)
    jsonschema.validate(manifest()["pack"], json.loads(path.read_text()))
    lock = json.loads(
        (PARENT / "contracts/knowlab-author-method-lock.json").read_text()
    )
    schema = PARENT.parent / lock["provider"] / lock["schema"]
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    definition = manifest()["pack"]["method_sources"]["report"]["definition"]
    jsonschema.validate(definition, json.loads(schema.read_text()))
