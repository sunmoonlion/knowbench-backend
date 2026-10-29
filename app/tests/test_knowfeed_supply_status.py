"""收 knowfeed 推来的需求状态（契约 knowfeed-supply-status/v1，knowfeed 定）：锁与提供方
schema 一致，我们的模型收的就是 schema 列的；入口只认 knowfeed 的服务身份。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.bootstrap.api import create_app
from app.domain.workbench.supply_status import (
    CONTRACT,
    RECEIVER_PATH,
    RELATION,
    DemandStatusEvent,
)
from app.infrastructure.security.relation_auth import relation_bindings
from core.config import Settings

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowfeed-supply-status-lock.json"
NAME = "demand-status.schema.json"


def _lock() -> dict:
    if not LOCK.exists():
        pytest.skip("parent contracts/ is not checked out")
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _provider(lock: dict, relative: str) -> Path:
    root = os.environ.get("KNOWFEED_SUPPLY_STATUS_CONTRACT_DIR")
    path = (
        Path(root) / Path(relative).name
        if root
        else PARENT.parent / "knowfeed-app" / relative
    )
    if not path.exists():
        pytest.skip("knowfeed-app is not checked out next to knowbench-app")
    return path


def sample(**change) -> dict:
    return {
        "contract": CONTRACT,
        "event_id": "ev-1",
        "demand_id": "d-1",
        "app": "knowbench",
        "owner": "user-123",
        "task_ref": "knowbench:k-1",
        "status": "partial",
        "items": [
            {
                "item_id": "i1",
                "confirmation_item": "c1",
                "status": "ready",
                "updated_at": "2026-10-29T08:00:00Z",
            },
            {
                "item_id": "i2",
                "confirmation_item": "c2",
                "status": "unsatisfiable",
                "reason": "no_channel",
                "updated_at": "2026-10-29T08:00:00Z",
            },
        ],
        "occurred_at": "2026-10-29T08:00:01Z",
    } | change


def test_the_lock_matches_the_provider() -> None:
    lock = _lock()
    assert lock["provider"] == "knowfeed-app" and lock["major_version"] == 1
    assert lock["contract"] == "knowfeed-supply-status"
    pinned = lock["schemas"][NAME]
    path = _provider(lock, pinned["path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == pinned["sha256"]
    manifest = json.loads(
        (path.parent / "contract-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schemas"][NAME] == pinned["sha256"]
    # 接收端入口与关系照提供方写的
    assert manifest["receiver_path"] == lock["receiver_path"] == RECEIVER_PATH
    assert manifest["relation"] == lock["relation"] == RELATION


def test_our_model_takes_exactly_what_the_schema_lists() -> None:
    """不依赖 jsonschema 也核：字段、必填、状态的取值与提供方一样。"""
    lock = _lock()
    schema = json.loads(
        _provider(lock, lock["schemas"][NAME]["path"]).read_text(encoding="utf-8")
    )
    ours = DemandStatusEvent.model_json_schema()
    item = schema["$defs"]["DemandStatusItemV1"]
    our_item = ours["$defs"]["DemandStatusItem"]
    assert set(ours["properties"]) == set(schema["properties"])
    assert set(our_item["properties"]) == set(item["properties"])
    assert set(item["required"]) <= set(our_item["required"])
    assert set(schema["required"]) <= set(ours["required"]) | {"contract"}
    assert schema["properties"]["app"]["enum"] == ["knowbench", "knowlab"]
    assert (
        schema["properties"]["status"]["enum"]
        == item["properties"]["status"]["enum"]
        == list(DemandStatusEvent.model_fields["status"].annotation.__args__)
    )
    DemandStatusEvent.model_validate(sample())
    for wrong in (
        sample(items=[]),
        sample(status="done"),
        sample(extra=1),
        sample(contract="knowfeed-supply-status/v2"),
    ):
        with pytest.raises(ValidationError):
            DemandStatusEvent.model_validate(wrong)
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(sample(), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(sample(items=[]), schema)


def test_only_knowfeed_may_push_demand_status() -> None:
    env = {
        "SERVICE_AUTH_SUPPLY_STATUS_CALLERS": "knowfeed,knowlab",
        "SERVICE_AUTH_SUPPLY_STATUS_KNOWFEED_AUDIENCE": "aud-knowfeed",
        "SERVICE_AUTH_SUPPLY_STATUS_KNOWFEED_SUBJECT_ALLOWLIST": "admin/knowfeed",
        "SERVICE_AUTH_SUPPLY_STATUS_KNOWFEED_REQUIRED_SCOPE": "knowbench:supply-status",
        "SERVICE_AUTH_SUPPLY_STATUS_KNOWLAB_AUDIENCE": "aud-knowlab",
        "SERVICE_AUTH_SUPPLY_STATUS_KNOWLAB_SUBJECT_ALLOWLIST": "admin/knowlab",
        "SERVICE_AUTH_SUPPLY_STATUS_KNOWLAB_REQUIRED_SCOPE": "knowbench:supply-status",
    }
    [binding] = relation_bindings("supply-status", env)
    assert (binding.caller, binding.audience) == ("knowfeed", "aud-knowfeed")
    assert binding.required_scope == "knowbench:supply-status"
    wrong = env | {
        "SERVICE_AUTH_SUPPLY_STATUS_KNOWFEED_REQUIRED_SCOPE": "knowbench:testrun"
    }
    assert relation_bindings("supply-status", wrong) == []


async def test_without_the_relation_the_receiver_refuses(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("SERVICE_AUTH_SUPPLY_STATUS"):
            monkeypatch.delenv(key)
    app = create_app(Settings())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as h:
        assert (await h.post(RECEIVER_PATH, json=sample())).status_code == 401
        refused = await h.post(
            RECEIVER_PATH, json=sample(), headers={"Authorization": "Bearer x"}
        )
        assert refused.status_code == 503
        assert refused.json()["code"] == "service_relation_unconfigured"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as h:  # 公网地址来的：当没有这个接口
        hidden = await h.post(
            RECEIVER_PATH, json=sample(), headers={"Authorization": "Bearer x"}
        )
        assert hidden.status_code == 404
