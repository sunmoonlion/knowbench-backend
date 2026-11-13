"""测试共用的夹具。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from sample_packs import SAMPLE_PACKS

from app.domain.workbench import packs


@pytest.fixture
def celery_probe_config(tmp_path, monkeypatch):
    """Use the deployed Celery queue policy in opt-in real broker probes."""
    if not os.environ.get("CELERY_PROBE_TEST_BROKER_URL"):
        return
    root = Path(__file__).resolve().parents[4]
    manifest = root / (
        "sunmoonun/gitops/components/app-platform/knowbench-app/"
        "knowbench-backend/runtime/workload.yaml"
    )
    assert manifest.is_file(), "broker probes require the platform deployment source"
    configs = [
        doc["data"]["celeryconfig.py"]
        for doc in yaml.safe_load_all(manifest.read_text())
        if isinstance(doc, dict)
        and doc.get("kind") == "ConfigMap"
        and "celeryconfig.py" in doc.get("data", {})
    ]
    assert len(configs) == 1, "expected one deployed Celery configuration"
    module = "broker_test_celeryconfig"
    (tmp_path / f"{module}.py").write_text(configs[0])
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("CELERY_CONFIG_MODULE", module)
    monkeypatch.setenv(
        "PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", "")
    )


@pytest.fixture(autouse=True)
def sample_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    """测委托机制的样例专家（tests/sample_packs.py，产品里 10-28 已撤）放进内置专家表：
    和撤之前一样是内部的（用户看不到、请不了）。"""
    for pack in SAMPLE_PACKS:
        monkeypatch.setitem(packs.BUILTIN_PACKS, pack.profile_id, pack)


@pytest.fixture
def builtin_packs_on_offer(sample_packs: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """测委托机制的测试借用样例专家：这里把它们临时放成可请的状态；撤下本身由
    test_builtin_experts_are_withdrawn_from_users 钉住。"""
    for pack in SAMPLE_PACKS:
        monkeypatch.setitem(
            packs.BUILTIN_PACKS,
            pack.profile_id,
            pack.model_copy(update={"internal": False}),
        )
