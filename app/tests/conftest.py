"""测试共用的夹具。"""

from __future__ import annotations

import pytest
from sample_packs import SAMPLE_PACKS

from app.domain.workbench import packs


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
