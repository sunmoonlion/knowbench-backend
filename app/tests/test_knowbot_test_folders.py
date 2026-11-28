"""账 129：经会合点服务流请测试电脑建、删每次测试的目录（假的服务流，不连库）。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

from app.application.workbench.agent_service import AgentRefused
from app.application.workbench.test_folders import (
    AgentTestFolders,
    FolderRefused,
    run_folder,
)
from app.domain.workbench.errors import WorkbenchError


class Repo:
    async def get_relay_identity(self, owner):
        return {"relay_user": "u-tester"} if owner == "tester" else None


@asynccontextmanager
async def stores():
    yield Repo()


class Service:
    def __init__(self, entries=None, refuse=None, fail=None, deny=None):
        self.entries, self.refuse, self.fail = entries, refuse, fail
        self.deny = deny
        self.calls: list[tuple[str, dict]] = []

    async def call(self, relay_user, method, params=None):
        assert relay_user == "u-tester"
        self.calls.append((method, params))
        if method == self.fail:
            raise WorkbenchError("agent offline")
        if method == self.deny:
            raise AgentRefused(-32010, "outside the whitelist")
        if method == "fs/readDirectory":
            if self.refuse:
                raise AgentRefused(
                    {"not_found": -32012, "denied": -32010}[self.refuse], "x"
                )
            return {"entries": self.entries or []}
        return {}


def test_folder_name_starts_with_when_it_was_accepted():
    run = {
        "created_at": datetime(2026, 10, 18, 1, 2, 3, tzinfo=UTC),
        "run_id": "knowlab-run/7",
    }
    assert run_folder(run).startswith("runs/20261018010203-")
    assert run_folder(run).endswith("-knowlab-run-7")
    assert run_folder(dict(run, folder_scheme=1)) == "runs/20261018010203-knowlab-run-7"
    for left, right in [
        ("knowlab:a", "knowlab-a"),
        ("a" * 80 + "1", "a" * 80 + "2"),
        ("KNOWLAB-A", "knowlab-a"),
    ]:
        assert (
            run_folder(dict(run, run_id=left)).casefold()
            != run_folder(dict(run, run_id=right)).casefold()
        )


async def test_prepare_only_creates_owned_directories():
    service = Service(
        entries=[
            {"name": "archive", "type": "dir"},
            {"name": "20261017-old", "type": "dir"},
            {"name": "mine", "type": "dir"},
            {"name": "note.txt", "type": "file"},
        ]
    )
    folders = AgentTestFolders(service, stores)
    assert await folders.prepare(owner="tester", root="/workspace", folder="runs/mine")
    assert service.calls == [
        ("fs/createDirectory", {"path": "/workspace/runs/mine", "recursive": True}),
        ("fs/createDirectory", {"path": "/workspace/runs/.tmp", "recursive": True}),
    ]


async def test_prepare_on_a_fresh_computer_just_makes_the_folder():
    service = Service(refuse="not_found")
    folders = AgentTestFolders(service, stores)
    assert await folders.prepare(owner="tester", root="/workspace", folder="runs/a")
    assert service.calls[-2][0] == "fs/createDirectory"


async def test_prepare_fails_closed_when_the_computer_cannot_be_reached_or_cleaned():
    assert not await AgentTestFolders(
        Service(fail="fs/createDirectory"), stores
    ).prepare(owner="tester", root="/workspace", folder="runs/a")
    assert not await AgentTestFolders(Service(), stores).prepare(
        owner="nobody", root="/workspace", folder="runs/a"
    )


async def test_creation_refusal_identifies_the_folder():
    blocked = Service(deny="fs/createDirectory")
    try:
        await AgentTestFolders(blocked, stores).prepare(
            owner="tester", root="/workspace", folder="runs/a"
        )
    except FolderRefused as exc:
        assert exc.message.startswith("/workspace/runs/a: ")
    else:
        raise AssertionError("expected FolderRefused")


async def test_remove_never_raises():
    service = Service(fail="fs/remove")
    await AgentTestFolders(service, stores).remove(
        owner="tester", root="/workspace", folder="runs/a"
    )
    assert service.calls == [("fs/remove", {"path": "/workspace/runs/a"})]
