"""跑孵化的一轮的契约（knowlab 定）：锁与提供方 schema 一致，我们收的请求、回的结果都合 schema，
交回物的规矩与提供方一样严。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_knowbot_incubations_db import HANDBACK, request

from app.application.workbench.incubations import view
from app.domain.workbench.incubations import Handback, IncubationRequest, material

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowlab-incubation-lock.json"
TASK = "03e447a0-0000-4000-8000-000000000000"


def _lock() -> dict:
    if not LOCK.exists():
        pytest.skip("parent contracts/ is not checked out")
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _schema(lock: dict, name: str) -> dict:
    root = os.environ.get("KNOWLAB_INCUBATION_CONTRACT_DIR")
    path = (
        Path(root) / name
        if root
        else PARENT.parent / "knowlab-app" / lock["schemas"][name]["path"]
    )
    if not path.exists():
        pytest.skip("knowlab-app is not checked out next to knowbench-app")
    return json.loads(path.read_text(encoding="utf-8"))


class OneTask:
    """view 只读这一轮的委托（花到多少、是不是还在排队）。"""

    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return self.task


def row(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "incubation_id": "knowlab-inc-0001",
        "status": status,
        "spent": Decimal("0"),
        "error": None,
        "task_id": None,
        "waiting_for": None,
        "started_at": None,
        "finished_at": None,
        "handback": None,
    } | extra


def test_every_pinned_schema_matches_the_provider() -> None:
    lock = _lock()
    assert lock["provider"] == "knowlab-app" and lock["major_version"] == 1
    assert lock["contract"] == "knowbot-incubation"
    for name, pinned in lock["schemas"].items():
        path = PARENT.parent / "knowlab-app" / pinned["path"]
        if not path.exists():
            pytest.skip("knowlab-app is not checked out next to knowbench-app")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pinned["sha256"], name


async def test_requests_we_accept_and_results_we_return_fit_the_contract() -> None:
    lock = _lock()
    jsonschema = pytest.importorskip("jsonschema")
    request_schema = _schema(lock, "incubation-request.schema.json")
    for action in ("draft", "revise"):
        sample = request(action=action).model_dump(mode="json")
        jsonschema.validate(sample, request_schema)
    sample = request(action="revise").model_dump(mode="json")
    for wrong in (
        {k: v for k, v in sample.items() if k != "runner"},  # 都在作者的电脑上跑
        {k: v for k, v in sample.items() if k != "instruction"},  # 按指令改要有那句话
        sample | {"data": [{"营业收入": 1}]},  # 只有列死的字段，不带数据内容
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(wrong, request_schema)
        with pytest.raises(ValidationError):
            IncubationRequest.model_validate(wrong)
    with pytest.raises(ValidationError):  # 零不是「不设上限」
        IncubationRequest.model_validate(sample | {"budget": {"limit": "0"}})
    result_schema = _schema(lock, "incubation-result.schema.json")
    now = datetime(2026, 10, 28, tzinfo=UTC)
    running = {"state": "RUNNING", "budget": {"used": "0.12"}, "original_input": {}}
    queued = {
        "state": "RECEIVED",
        "budget": {"used": "0"},
        "original_input": {"client_context": {"queued_behind": TASK}},
    }
    cases = [
        (row("queued"), None, "queued"),
        (row("queued", waiting_for="computer_offline"), None, "waiting"),
        (row("running", task_id=TASK, started_at=now), running, "running"),
        (row("running", task_id=TASK, started_at=now), queued, "queued"),
        (
            row(
                "running", task_id=TASK, started_at=now, waiting_for="computer_offline"
            ),
            running,
            "waiting",
        ),
        (
            row(
                "succeeded",
                task_id=TASK,
                started_at=now,
                finished_at=now,
                spent=Decimal("0.3"),
                handback=HANDBACK,
            ),
            None,
            "succeeded",
        ),
        (row("over_budget", error=None, finished_at=now), None, "over_budget"),
        (row("failed", error="timed out", finished_at=now), None, "failed"),
        (row("cancelled", finished_at=now), None, "cancelled"),
    ]
    for given, task, want in cases:
        got = await view(OneTask(task or {}), given)  # type: ignore[arg-type]
        assert got["status"] == want, (given, got)
        jsonschema.validate(got, result_schema)
    spent = await view(OneTask(running), row("running", task_id=TASK, started_at=now))  # type: ignore[arg-type]
    assert spent["cost"]["spent"] == "0.120000"  # 没结论时花费算到此刻


def test_handback_rules_are_as_strict_as_the_provider() -> None:
    """判断只出缺口：建议只碰方法层的格子，不碰工程层；出处按种类引；自查改成的缺口在缺口表里。
    提供方 schema 拒的我们也拒（schema 表达不了的跨项规矩在两边的模型里）。"""
    lock = _lock()
    jsonschema = pytest.importorskip("jsonschema")
    result_schema = _schema(lock, "incubation-result.schema.json")
    Handback.model_validate(HANDBACK)

    def with_(**change: Any) -> dict[str, Any]:
        body = copy.deepcopy(HANDBACK)
        body.update(change)
        return body

    suggestion = HANDBACK["suggestions"][0]
    by_schema = [
        with_(suggestions=[suggestion | {"path": "steps/scan/advanced"}]),
        with_(suggestions=[suggestion | {"path": "steps/scan/method_text"}]),
        with_(suggestions=[suggestion | {"path": "data_picks"}]),
        with_(suggestions=[suggestion | {"op": "rewrite"}]),
        with_(suggestions=[suggestion | {"reason": ""}]),
        with_(sources=[{"path": "name", "kind": "guess"}]),
        with_(gaps=[HANDBACK["gaps"][0] | {"proposal": ""}]),
    ]
    by_models = [
        with_(suggestions=[suggestion | {"value": {"advanced": {"tools": []}}}]),
        with_(suggestions=[suggestion | {"op": "remove"}]),
        with_(sources=[{"path": "name", "kind": "idea"}]),
        with_(sources=[{"path": "name", "kind": "answer"}]),
        with_(self_check=[{"suggestion_id": "s0", "turned_into_gap": "g9"}]),
        with_(self_check=[{"suggestion_id": "s1", "turned_into_gap": "g1"}]),
    ]
    base = {
        "contract": "knowbot-incubation/v1",
        "incubation_id": "knowlab-inc-0001",
        "status": "succeeded",
    }
    jsonschema.validate(base | {"handback": HANDBACK}, result_schema)
    for wrong in by_schema:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(base | {"handback": wrong}, result_schema)
    for wrong in by_schema + by_models:
        with pytest.raises(ValidationError):
            Handback.model_validate(wrong)


def test_the_model_sees_only_the_fields_the_contract_lists() -> None:
    """0029 第 6.4 节：交给模型的只有动作与指令、创意、现稿、答复、数据的格式。"""
    given = material(request(action="revise"))
    assert set(given) == {
        "action",
        "instruction",
        "seed",
        "workdraft",
        "answers",
        "data_formats",
    }
    assert "step_id" not in given  # 没指定哪一步就不带
