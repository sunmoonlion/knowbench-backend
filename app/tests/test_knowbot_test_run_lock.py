"""跑分身测试的契约（knowlab 定）：锁与提供方 schema 一致，我们收的请求、回的结果都合 schema。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from knowbot_fixtures import manifest

from app.domain.workbench.test_runs import RunCase, case_outputs, case_result

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowlab-test-run-lock.json"


def _lock() -> dict:
    if not LOCK.exists():
        pytest.skip("parent contracts/ is not checked out")
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _schema(lock: dict, name: str) -> dict:
    root = os.environ.get("KNOWLAB_TEST_RUN_CONTRACT_DIR")
    path = (
        Path(root) / name
        if root
        else PARENT.parent / "knowlab-app" / lock["schemas"][name]["path"]
    )
    if not path.exists():
        pytest.skip("knowlab-app is not checked out next to knowbench-app")
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_pinned_schema_matches_the_provider() -> None:
    lock = _lock()
    assert lock["provider"] == "knowlab-app" and lock["major_version"] == 1
    for name, pinned in lock["schemas"].items():
        path = PARENT.parent / "knowlab-app" / pinned["path"]
        if not path.exists():
            pytest.skip("knowlab-app is not checked out next to knowbench-app")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pinned["sha256"], name


def test_requests_we_accept_and_results_we_return_fit_the_contract() -> None:
    lock = _lock()
    jsonschema = pytest.importorskip("jsonschema")
    case = {
        "case_id": "c1",
        "input": {"question": "看看这家公司", "answers": {"period": "3y"}},
        "expectations": [
            {"artifact": "answer", "path": "answer", "kind": "contains", "value": "x"}
        ],
    }
    request = {
        "contract": "knowbot-test-run/v1",
        "run_id": "knowlab-run-0001",
        "owner_subject": "user-123",
        "manifest": manifest(),
        "cases": [case],
        "budget": {"limit": "20"},
        "timeout_seconds": 1800,
        "max_wait_seconds": 259200,
    }
    jsonschema.validate(request, _schema(lock, "test-run-request.schema.json"))
    passed = case_result(
        case=RunCase.model_validate(case),
        task={"state": "SUCCEEDED"},
        steps=[
            {
                "step_id": "s1",
                "status": "accepted",
                "attempts": [{"outcome": "accepted", "checks": [{"pass": True}]}],
            }
        ],
        artifacts={"answer": {"answer": "x"}},
    )
    result_schema = _schema(lock, "test-run-result.schema.json")
    base = {
        "contract": "knowbot-test-run/v1",
        "run_id": "knowlab-run-0001",
        "cost": {"spent": "0.120000"},
        "started_at": "2026-10-17T00:00:00+00:00",
        "finished_at": None,
        "waited_seconds": 0,
    }
    jsonschema.validate(
        base | {"status": "passed", "cases": [passed], "questions": []}, result_schema
    )
    # 没过的带原因（审读第 3 条）；平台取不到数记成出错
    failed = case_result(
        case=RunCase.model_validate(case),
        task={"state": "FAILED"},
        steps=[
            {
                "step_id": "answer",
                "status": "rejected",
                "attempts": [
                    {
                        "outcome": "rejected",
                        "checks": [
                            {
                                "rule": "sourced",
                                "label": "取出的数与表里一致",
                                "pass": False,
                                "detail": "user_data_unavailable: not configured",
                                "platform": True,
                            }
                        ],
                    }
                ],
            }
        ],
        artifacts={},
    )
    assert failed["status"] == "error" and failed["steps"][0]["details"]
    jsonschema.validate(
        base | {"status": "error", "cases": [failed], "questions": []}, result_schema
    )
    waiting = base | {
        "status": "waiting",
        "cases": [],
        "questions": [
            {
                "question_id": "6f1c:period",
                "case_id": "c1",
                "step_id": "plan",
                "kind": "ask_before",
                "key": "period",
                "text": "看哪几年？",
                "options": [{"id": "3y", "label": "近三年"}],
                "allow_free_text": True,
                "asked_at": "2026-10-17T00:00:00+00:00",
            }
        ],
    }
    jsonschema.validate(waiting, result_schema)
    # 没结论时带进度（待办账 135）：形状同 KnowbotTestService.progress
    progress = {
        "case_id": "c1",
        "cases_done": 0,
        "cases_total": 1,
        "step": 1,
        "steps_total": 2,
        "step_title": "定范围",
        "attempt": 1,
        "spent": "0.000000",
    }
    jsonschema.validate(waiting | {"progress": progress}, result_schema)
    answers = {
        "contract": "knowbot-test-run/v1",
        "answers": [
            {"question_id": "6f1c:period", "option_id": "3y", "answered_by": "user"}
        ],
    }
    jsonschema.validate(answers, _schema(lock, "test-run-answers.schema.json"))


def test_case_outputs_we_return_fit_the_contract() -> None:
    """SDD 0037：一条用例的产出合提供方 schema（成稿、通过的交回物、没过的原文、没走到的）。"""
    lock = _lock()
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema(lock, "test-run-case-outputs.schema.json")
    out = case_outputs(
        run_id="knowlab-run-0001",
        case_id="c1",
        steps=[
            {
                "step_id": "plan",
                "title": "定计划",
                "attempts": [
                    {
                        "outcome": "accepted",
                        "checks": [{"label": "格式对", "pass": True, "message": ""}],
                        "returned": {"content": {"plan": ["a"]}},
                    }
                ],
            },
            {
                "step_id": "answer",
                "title": "回答",
                "attempts": [
                    {
                        "outcome": "rejected",
                        "checks": [
                            {"label": "有出处", "pass": False, "message": "没有"}
                        ],
                        "returned": {"content": None, "raw": "not json"},
                    }
                ],
            },
            {"step_id": "writeup", "title": "成稿", "attempts": []},
        ],
        deliverable={"path": "底稿/成稿.md", "text": "# 成稿", "truncated": False},
        deliverable_note=None,
    )
    jsonschema.validate(out, schema)
    nothing = case_outputs(
        run_id="knowlab-run-0001",
        case_id="c1",
        steps=[],
        deliverable=None,
        deliverable_note="这条用例没有写成稿",
        error="the outputs could not be collected: RuntimeError",
    )
    jsonschema.validate(nothing, schema)
