"""测试共用的夹具。"""

from __future__ import annotations

import pytest

from app.domain.workbench import packs


@pytest.fixture
def builtin_packs_on_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    """内置专家 2026-10-18 已对用户撤下（待办账 111，数据拓扑）。测委托机制的测试借用它们：
    这里把副本临时放回可请的状态；撤下本身由 test_builtin_experts_are_withdrawn_from_users 钉住。"""
    for pid in ("DATA_QUERY", "FIN_REVIEW", "FIN_FORECAST"):
        pack = packs.BUILTIN_PACKS[pid]
        monkeypatch.setitem(
            packs.BUILTIN_PACKS, pid, pack.model_copy(update={"internal": False})
        )
