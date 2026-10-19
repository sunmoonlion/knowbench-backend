"""成稿硬检查（0022 段 4）：路径拼法、三种回答怎么合进验收结论、经服务流问代理的映射；
测试的用例跑完时读回成稿（SDD 0037）。"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager

import pytest

from app.application.workbench.acceptance import Verdict
from app.application.workbench.agent_service import (
    MAX_READ_BYTES,
    AgentOffline,
    AgentRefused,
)
from app.application.workbench.deliverable_check import (
    AgentDeliverableCheck,
    DeliverableTooLarge,
    harden,
    join_path,
)


def test_join_path_follows_the_project_directory_style():
    assert (
        join_path("C:\\r\\恒瑞医药", "底稿/财报体检-成稿.md")
        == "C:\\r\\恒瑞医药\\底稿\\财报体检-成稿.md"
    )
    assert join_path("C:\\r\\p\\", "交回.md") == "C:\\r\\p\\交回.md"
    assert join_path("\\\\nas\\share\\p", "a/b.md") == "\\\\nas\\share\\p\\a\\b.md"
    assert join_path("/home/u/p", "底稿/x.md") == "/home/u/p/底稿/x.md"
    assert join_path("/home/u/p/", "/x.md") == "/home/u/p/x.md"


def test_harden_keeps_soft_when_unknown_and_fails_when_agent_says_missing():
    ok = Verdict(ok=True, parsed={"a": 1}, failures=[], checks=[{"c": 1}])
    assert harden(ok, written=True, found=None, output_file="交回.md") == (
        ok,
        True,
        "events",
    )
    assert harden(ok, written=False, found=True, output_file="交回.md") == (
        ok,
        True,
        "agent",
    )
    assert harden(ok, written=False, found=None, output_file="交回.md") == (
        ok,
        False,
        "events",
    )
    failed, written, who = harden(ok, written=False, found=False, output_file="交回.md")
    assert (written, who) == (False, "agent")
    assert (
        failed.ok is False and failed.parsed == {"a": 1} and failed.checks == [{"c": 1}]
    )
    assert failed.failures == ["成稿文件没有写到 交回.md"]
    # 原结论没被改动
    assert ok.ok is True and ok.failures == []


class FakeRepo:
    def __init__(self, identity):
        self.identity = identity

    async def get_relay_identity(self, owner):
        return self.identity


def stores_with(identity):
    @asynccontextmanager
    async def stores():
        yield FakeRepo(identity)

    return stores


class FakeService:
    def __init__(self, result=None, raise_=None):
        self.result, self.raise_, self.calls = result, raise_, []

    async def call(self, relay_user, method, params=None):
        self.calls.append((relay_user, method, params))
        if self.raise_:
            raise self.raise_
        return self.result


async def test_agent_check_asks_for_metadata_under_the_project_directory():
    service = FakeService({"type": "file", "size": 3, "mtime": 1})
    check = AgentDeliverableCheck(service, stores_with({"relay_user": "u-abc"}))
    assert await check.exists(owner="o", directory="C:\\r\\p", path="底稿/x.md") is True
    assert service.calls == [
        ("u-abc", "fs/getMetadata", {"path": "C:\\r\\p\\底稿\\x.md"})
    ]
    service.result = {"type": "dir"}
    assert await check.exists(owner="o", directory="C:\\r\\p", path="底稿") is False


@pytest.mark.parametrize(
    "exc,expected",
    [
        (AgentRefused(-32012, "not_found"), False),
        (AgentRefused(-32010, "outside"), None),
        (AgentOffline("off"), None),
    ],
)
async def test_agent_check_maps_refusals(exc, expected):
    check = AgentDeliverableCheck(
        FakeService(raise_=exc), stores_with({"relay_user": "u-abc"})
    )
    assert await check.exists(owner="o", directory="/p", path="交回.md") is expected


async def test_agent_check_is_unknown_without_pairing_or_directory():
    service = FakeService({"type": "file"})
    assert (
        await AgentDeliverableCheck(service, stores_with(None)).exists(
            owner="o", directory="/p", path="x"
        )
        is None
    )
    assert (
        await AgentDeliverableCheck(service, stores_with({"relay_user": "u"})).exists(
            owner="o", directory=None, path="x"
        )
        is None
    )
    assert service.calls == []


async def test_reading_the_deliverable_decodes_one_read_and_refuses_bigger_files():
    text = "# 成稿\n\n营业收入逐年上升。"
    service = FakeService(
        {
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "size": len(text.encode("utf-8")),
            "mtime": 1,
            "eof": True,
        }
    )
    reader = AgentDeliverableCheck(service, stores_with({"relay_user": "u-abc"}))
    found = await reader.read(owner="o", directory="/workspace", path="底稿/x.md")
    assert found is not None and (found.text, found.mtime_ms) == (text, 1)
    assert service.calls == [
        (
            "u-abc",
            "fs/readFile",
            {"path": "/workspace/底稿/x.md", "offset": 0, "length": MAX_READ_BYTES},
        )
    ]
    service.result = {"content": "", "size": MAX_READ_BYTES + 1, "eof": False}
    with pytest.raises(DeliverableTooLarge):
        await reader.read(owner="o", directory="/workspace", path="底稿/x.md")


async def test_reading_without_pairing_is_unknown_and_refusals_propagate():
    assert (
        await AgentDeliverableCheck(FakeService({}), stores_with(None)).read(
            owner="o", directory="/p", path="x"
        )
        is None
    )
    offline = AgentDeliverableCheck(
        FakeService(raise_=AgentOffline("off")), stores_with({"relay_user": "u"})
    )
    with pytest.raises(AgentOffline):
        await offline.read(owner="o", directory="/p", path="x")
