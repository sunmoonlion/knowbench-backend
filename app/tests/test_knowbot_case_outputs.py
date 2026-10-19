"""一条用例的产出（SDD 0037）：从步骤视图取每步最后一次尝试，按上限截。纯函数。"""

from __future__ import annotations

from app.domain.workbench.test_runs import (
    ARTIFACT_BYTES,
    DELIVERABLE_CHARS,
    OUTPUTS_BYTES,
    RAW_CHARS,
    _json_bytes,
    case_outputs,
    step_output,
)


def _step(step_id: str, *attempts: dict) -> dict:
    return {"step_id": step_id, "title": f"第 {step_id} 步", "attempts": list(attempts)}


def test_each_step_takes_its_last_attempt() -> None:
    accepted = step_output(
        _step(
            "plan",
            {"outcome": "rejected", "checks": [], "returned": {"content": {"x": 0}}},
            {
                "outcome": "accepted",
                "checks": [{"label": "格式对", "pass": True, "message": ""}],
                "returned": {"artifact": "plan", "content": {"plan": ["a"]}},
            },
        )
    )
    assert accepted == {
        "step_id": "plan",
        "title": "第 plan 步",
        "outcome": "accepted",
        "checks": [{"label": "格式对", "passed": True, "message": ""}],
        "artifact": {"plan": ["a"]},
    }
    rejected = step_output(
        _step(
            "answer",
            {
                "outcome": "rejected",
                "checks": [{"label": "有出处", "pass": False, "message": "没有出处"}],
                "returned": {"content": None, "raw": "x" * (RAW_CHARS + 5)},
            },
        )
    )
    assert rejected["outcome"] == "rejected"
    assert "artifact" not in rejected
    assert rejected["returned_raw"] == "x" * RAW_CHARS
    assert rejected["checks"] == [
        {"label": "有出处", "passed": False, "message": "没有出处"}
    ]
    assert step_output(_step("later")) == {
        "step_id": "later",
        "title": "第 later 步",
        "outcome": "not_reached",
        "checks": [],
    }


def test_a_step_cut_short_has_no_raw_and_an_unknown_outcome_is_running() -> None:
    failed = step_output(_step("s", {"outcome": "failed", "checks": []}))
    assert failed["outcome"] == "failed" and "returned_raw" not in failed
    odd = step_output(_step("s", {"outcome": "something-new", "checks": []}))
    assert odd["outcome"] == "running"


def test_an_artifact_over_the_limit_is_omitted_with_its_size() -> None:
    big = {"rows": ["数" * 1000] * (ARTIFACT_BYTES // 3000 + 1)}
    out = step_output(
        _step(
            "series",
            {"outcome": "accepted", "checks": [], "returned": {"content": big}},
        )
    )
    assert "artifact" not in out
    assert out["artifact_omitted_bytes"] == _json_bytes(big) > ARTIFACT_BYTES


def test_a_long_deliverable_is_cut_and_marked() -> None:
    out = case_outputs(
        run_id="knowlab-run-0001",
        case_id="c1",
        steps=[],
        deliverable={
            "path": "底稿/成稿.md",
            "text": "字" * (DELIVERABLE_CHARS + 1),
            "truncated": False,
        },
        deliverable_note=None,
    )
    assert out["deliverable"]["truncated"] is True
    assert len(out["deliverable"]["text"]) == DELIVERABLE_CHARS
    assert out["contract"] == "knowbot-test-run/v1" and out["error"] is None


def test_over_the_total_the_biggest_artifacts_go_first_then_the_deliverable() -> None:
    near = {"rows": ["数" * 1000] * (ARTIFACT_BYTES // 3100)}  # 每步都在单步上限内
    steps = [
        _step(
            f"s{n}",
            {"outcome": "accepted", "checks": [], "returned": {"content": near}},
        )
        for n in range(12)
    ]
    out = case_outputs(
        run_id="knowlab-run-0001",
        case_id="c1",
        steps=steps,
        deliverable={
            "path": "a.md",
            "text": "稿" * DELIVERABLE_CHARS,
            "truncated": False,
        },
        deliverable_note=None,
    )
    assert _json_bytes(out) <= OUTPUTS_BYTES
    omitted = [s for s in out["steps"] if "artifact_omitted_bytes" in s]
    kept = [s for s in out["steps"] if "artifact" in s]
    assert omitted and kept  # 只拿掉够用的几步，不是全拿掉
    assert out["deliverable"]["truncated"] is False  # 交回物让出来就够了，成稿不截


def test_notes_are_capped() -> None:
    out = case_outputs(
        run_id="knowlab-run-0001",
        case_id="c1",
        steps=[],
        deliverable=None,
        deliverable_note="没" * 600,
        error="错" * 600,
    )
    assert len(out["deliverable_note"]) == 500 and len(out["error"]) == 500
