"""声明 v2 照 knowbase 的契约写：锁与真源一致，本仓模型生成的 schema 与真源一致。

只比结构，不比说明文字（说明是给人看的，两边各写各的中文）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.domain.workbench.data_needs_v2 import DataNeed

PARENT = Path(__file__).resolve().parents[3]
LOCK = PARENT / "contracts/knowbase-data-needs-lock.json"


def _lock() -> dict:
    if not LOCK.exists():  # 只拿到后端子仓时没有父仓
        pytest.skip("parent contracts/ is not checked out")
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _provider(lock: dict) -> Path:
    configured = os.environ.get("KNOWBASE_DATA_NEEDS_SCHEMA_PATH")
    path = (
        Path(configured)
        if configured
        else PARENT.parent / "knowbase-app" / lock["schema"]
    )
    if not path.exists():
        pytest.skip("knowbase-app is not checked out next to knowbench-app")
    return path


def _structure(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _structure(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_structure(v) for v in node]
    return node


def test_lock_matches_the_provider() -> None:
    lock = _lock()
    assert (lock["provider"], lock["contract"], lock["major_version"]) == (
        "knowbase-app",
        "knowbot-data-needs",
        2,
    )
    assert hashlib.sha256(_provider(lock).read_bytes()).hexdigest() == lock["sha256"]


def test_our_model_is_the_provider_model() -> None:
    provider = json.loads(_provider(_lock()).read_text(encoding="utf-8"))
    ours = DataNeed.model_json_schema(ref_template="#/$defs/{model}")
    defs = ours.pop("$defs", {})
    defs["DataNeed"] = ours
    assert _structure(defs) == _structure(provider["$defs"])
