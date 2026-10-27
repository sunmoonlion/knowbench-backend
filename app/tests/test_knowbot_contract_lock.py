"""从 knowmart 请来的分身清单：锁与提供方 schema 一致，工作台认的样例清单也合提供方 schema。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from knowbot_fixtures import KNOWBOT, manifest

from app.domain.workbench.knowbots import pack_from_manifest

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowmart-knowbot-manifest-lock.json"


def _lock() -> dict:
    if not LOCK.exists():  # 只拿到后端子仓时没有父仓
        pytest.skip("parent contracts/ is not checked out")
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _provider_schema(lock: dict) -> Path:
    configured = os.environ.get("KNOWMART_KNOWBOT_MANIFEST_SCHEMA_PATH")
    path = (
        Path(configured)
        if configured
        else PARENT.parent / "knowmart-app" / lock["schema"]
    )
    if not path.exists():
        pytest.skip("knowmart-app is not checked out next to knowbench-app")
    return path


def test_lock_matches_the_provider_schema() -> None:
    lock = _lock()
    assert lock["provider"] == "knowmart-app"
    assert lock["contract"] == "knowbot-manifest" and lock["major_version"] == 1
    schema = _provider_schema(lock)
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]


def test_a_manifest_the_workbench_runs_satisfies_the_provider_schema() -> None:
    schema = _provider_schema(_lock())
    jsonschema = pytest.importorskip("jsonschema")
    doc = manifest()
    pack_from_manifest(doc, knowbot_id=KNOWBOT, version="1")
    jsonschema.validate(doc, json.loads(schema.read_text(encoding="utf-8")))


LOCK_V2 = PARENT / "contracts/knowmart-knowbot-manifest-v2-lock.json"


def test_the_v2_lock_matches_the_provider_schema() -> None:
    """清单 v2（SDD 0043）：锁钉住 knowmart 的 v2 schema；工作台认的 v2 样例也合它。"""
    if not LOCK_V2.exists():
        pytest.skip("parent contracts/ is not checked out")
    lock = json.loads(LOCK_V2.read_text(encoding="utf-8"))
    assert lock["contract"] == "knowbot-manifest" and lock["major_version"] == 2
    schema = _provider_schema(lock)
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == lock["sha256"]
    jsonschema = pytest.importorskip("jsonschema")
    doc = {
        **manifest(),
        "schema": "knowbot-manifest/v2",
        "data_needs": [
            {
                "key": "annual",
                "name": "年报",
                "doc_type": "annual_report",
                "period": {"kind": "latest", "count": 2},
                "formats": ["pdf"],
            }
        ],
        "bindings": [],
    }
    doc.pop("data_bindings", None)
    pack_from_manifest(doc, knowbot_id=KNOWBOT, version="1")
    jsonschema.validate(doc, json.loads(schema.read_text(encoding="utf-8")))
