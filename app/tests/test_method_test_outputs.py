"""Provider schema, stable expectations and bounded dynamic evidence envelopes."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from test_knowbot_test_run_lock import _lock, _schema

from app.domain.workbench.test_runs import OUTPUTS_BYTES, RunRequest, case_outputs


def request():
    return {
        "contract": "knowbot-test-run/v1",
        "run_id": "synthetic-method-test",
        "owner_subject": "author",
        "runner": {
            "kind": "user",
            "environment_id": "00000000-0000-0000-0000-000000000001",
            "workspace_root": "/work",
        },
        "manifest": json.loads(
            (
                Path(__file__).parent / "fixtures/dynamic-methods-manifest.json"
            ).read_text()
        ),
        "cases": [
            {
                "case_id": "c1",
                "input": {"question": "synthetic"},
                "expectations": [
                    {
                        "artifact": "report",
                        "path": "rows.0.id",
                        "kind": "equals",
                        "value": "one",
                    }
                ],
            }
        ],
        "budget": {"limit": "1"},
        "timeout_seconds": 600,
    }


def test_dynamic_expectations_use_declared_final_method_names_only():
    body = request()
    assert RunRequest.model_validate(body).cases[0].expectations[0].artifact == "report"
    jsonschema.validate(body, _schema(_lock(), "test-run-request.schema.json"))
    for name in ("inspect", "report_1", "__method_output__:report_1", "missing"):
        invalid = copy.deepcopy(body)
        invalid["cases"][0]["expectations"][0]["artifact"] = name
        with pytest.raises(ValidationError, match="declared final"):
            RunRequest.model_validate(invalid)
    for pack in (None, [], {"planning": []}, {"planning": {"final_units": [{}]}}):
        invalid = copy.deepcopy(body)
        invalid["manifest"]["pack"] = pack
        with pytest.raises(ValidationError, match="declared final"):
            RunRequest.model_validate(invalid)


def test_output_budget_retains_all_final_references_and_does_not_mutate_inputs():
    finals = [
        {
            "method_id": f"method_{n}",
            "instance_id": f"instance_{n}",
            "result": {
                "id": f"00000000-0000-0000-0000-{n:012d}",
                "version": 3,
                "digest": "b" * 64,
            },
            "artifact": {"text": "合成" * 20_000},
            "deliverable": {
                "path": f"document-{n}.md",
                "text": "合成" * 100_001,
                "step_id": f"instance_{n}",
            },
        }
        for n in range(30)
    ]
    original = copy.deepcopy(finals)
    out = case_outputs(
        run_id="synthetic-method-test",
        case_id="c1",
        steps=[
            {
                "step_id": "historical",
                "title": "synthetic",
                "current_result": None,
                "attempts": [
                    {"outcome": "accepted", "returned": {"content": {"old": True}}}
                ],
            }
        ],
        deliverable=None,
        deliverable_note=None,
        plan_version=4,
        final_outputs=finals,
    )
    jsonschema.validate(out, _schema(_lock(), "test-run-case-outputs.schema.json"))
    assert len(json.dumps(out, ensure_ascii=False).encode()) < OUTPUTS_BYTES
    assert len(out["final_outputs"]) == 30
    assert [f["result"] for f in out["final_outputs"]] == [
        f["result"] for f in original
    ]
    assert out["steps"][0]["current"] is False
    assert any(f.get("artifact_omitted_bytes") for f in out["final_outputs"])
    assert any(f.get("deliverable") is None for f in out["final_outputs"])
    assert finals == original


def test_unaccepted_final_has_no_content_and_large_accepted_artifact_is_omitted():
    finals = [
        {
            "method_id": "first",
            "instance_id": "first_1",
            "result": None,
            "note": "not reached",
        },
        {
            "method_id": "last",
            "instance_id": "last_1",
            "result": {
                "id": "00000000-0000-0000-0000-000000000001",
                "version": 1,
                "digest": "c" * 64,
            },
            "artifact": "x" * 200_001,
        },
    ]
    out = case_outputs(
        run_id="synthetic-method-test",
        case_id="c1",
        steps=[],
        deliverable=None,
        deliverable_note=None,
        plan_version=1,
        final_outputs=finals,
    )
    jsonschema.validate(out, _schema(_lock(), "test-run-case-outputs.schema.json"))
    assert out["final_outputs"][0] == finals[0]
    assert "artifact" not in out["final_outputs"][1]
    assert out["final_outputs"][1]["artifact_omitted_bytes"] == 200_003
