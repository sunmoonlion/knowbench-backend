import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from test_method_test_outputs import request

from app.domain.workbench.test_runs import RunRequest


def test_v2_uses_unique_author_method_and_keeps_v1_final_only():
    raw = request()
    raw["manifest"]["pack"]["runtime_contract"] = "knowbench/dynamic-methods/v3"
    for unit in raw["manifest"]["pack"]["method_units"]:
        if unit["contract"]["step_id"] == "inspect":
            unit["selection"]["max_calls"] = 1
    raw["cases"][0]["expectations"][0]["artifact"] = "inspect"
    with pytest.raises(ValidationError, match="declared final"):
        RunRequest.model_validate(raw)
    raw["contract"] = "knowbot-test-run/v2"
    assert RunRequest.model_validate(raw).cases[0].expectations[0].artifact == "inspect"
    parent = Path(__file__).resolve().parents[3]
    lock = json.loads((parent / "contracts/knowlab-test-run-v2-lock.json").read_text())
    file = parent.parent / lock["provider"] / lock["request_schema"]
    assert hashlib.sha256(file.read_bytes()).hexdigest() == lock["sha256"]
    jsonschema.validate(raw, json.loads(file.read_text()))
    for name in ("review", "inspect_1", "missing"):
        raw["cases"][0]["expectations"][0]["artifact"] = name
        with pytest.raises(ValidationError):
            RunRequest.model_validate(raw)
    raw["cases"][0]["expectations"][0]["artifact"] = "report"
    raw["manifest"]["pack"]["runtime_contract"] = "knowbench/dynamic-methods/v2"
    with pytest.raises(ValidationError, match="v3 capability"):
        RunRequest.model_validate(raw)
