"""Consumer pin for provider-owned atomic meaning and expert-owned execution."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest

from app.domain.workbench.method_catalog import parse_catalog


def document():
    return json.loads(
        (Path(__file__).parent / "fixtures/atomic-methods-manifest.json").read_text()
    )["pack"]


def test_atomic_definition_provider_lock_and_compiled_fixture():
    parent = Path(__file__).resolve().parents[3]
    lock = json.loads(
        (parent / "contracts/knowlab-author-method-v3-lock.json").read_text()
    )
    source = parent.parent / lock["provider"] / lock["schema"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == lock["sha256"]
    pack = document()
    jsonschema.validate(
        pack["method_sources"]["report"]["definition"], json.loads(source.read_text())
    )
    parsed = parse_catalog(pack)
    assert parsed.model_dump(mode="json")["method_sources"] == pack["method_sources"]


@pytest.mark.parametrize(
    "damage", ["budget", "failure", "output", "questions", "slots", "meaning"]
)
def test_atomic_compilation_cannot_change_meaning_or_policy(damage):
    pack = deepcopy(document())
    unit = next(
        u["contract"]
        for u in pack["method_units"]
        if u["contract"]["step_id"] == "report"
    )
    source = pack["method_sources"]["report"]
    if damage == "budget":
        unit["reserve"] = "1.00"
    elif damage == "failure":
        unit["on_reject"] = "rework"
    elif damage == "output":
        unit["output_schema"] = {"invented": "string"}
    elif damage == "questions":
        source["reference"]["execution"]["ask_before"] = [
            {"id": "ask", "question": "新增问题", "options": []}
        ]
    elif damage == "slots":
        source["reference"]["inputs"]["records"] = "review"
    else:
        source["definition"]["procedure"] = "改写正文"
    with pytest.raises(ValueError):
        parse_catalog(pack)
