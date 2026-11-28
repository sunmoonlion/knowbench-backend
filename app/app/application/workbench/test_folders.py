"""分身测试的工作目录（账 129，所有者 2026-10-18「按你建议」）。

每次测试在作者工作区 runs 下使用自己的目录，完成后只清理本轮的目录。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStores
from app.application.workbench.agent_service import AgentRefused, AgentService
from app.application.workbench.deliverable_check import join_path
from app.domain.workbench.errors import WorkbenchError

RUNS = "runs"
TMP = ".tmp"  # runs 下给测试放临时文件的目录（平台电脑的 TMPDIR 指到这里）
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def run_folder(run: dict[str, Any]) -> str:
    """这次测试的目录（相对工作区）。受理时间在前，按名字排就是按先后。"""
    stamp = run["created_at"].strftime("%Y%m%d%H%M%S")
    identity = str(run["run_id"])
    suffix = _UNSAFE.sub("-", identity)[:80]
    if run.get("folder_scheme", 2) == 1:
        return f"{RUNS}/{stamp}-{suffix}"
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return f"{RUNS}/{stamp}-{fingerprint}-{suffix[:64]}"


class FolderRefused(WorkbenchError):
    """代理明确拒了删或建（不是离线、超时这类过一会儿就好的）：等下去也不会好，
    立刻报错，写明是哪个目录（审读第 15 条）。"""

    code = "test_folder_refused"


class TestFolders(Protocol):
    async def prepare(self, *, owner: str, root: str, folder: str) -> bool:
        """建好本轮自己的目录。建好了回 True；问不到回 False（下一轮再试）；
        代理拒了删或建抛 FolderRefused。"""
        ...

    async def remove(self, *, owner: str, root: str, folder: str) -> None:
        """清理本轮目录；电脑离线时保留目录。"""
        ...


class AgentTestFolders:
    def __init__(self, service: AgentService, stores: WorkbenchStores) -> None:
        self.service = service
        self.stores = stores

    async def _relay_user(self, owner: str) -> str | None:
        async with self.stores() as repo:
            identity = await repo.get_relay_identity(owner)
        relay_user = identity.get("relay_user") if identity else None
        return str(relay_user) if relay_user else None

    async def prepare(self, *, owner: str, root: str, folder: str) -> bool:
        relay_user = await self._relay_user(owner)
        if relay_user is None:
            return False
        runs = join_path(root, RUNS)
        target = join_path(root, folder)
        try:
            await self.service.call(
                relay_user, "fs/createDirectory", {"path": target, "recursive": True}
            )
            # 平台测试机的 TMPDIR 可能已存在。
            await self.service.call(
                relay_user,
                "fs/createDirectory",
                {"path": join_path(runs, TMP), "recursive": True},
            )
        except AgentRefused as exc:
            raise FolderRefused(f"{target}: {exc.code}") from None
        except WorkbenchError:
            return False
        return True

    async def remove(self, *, owner: str, root: str, folder: str) -> None:
        relay_user = await self._relay_user(owner)
        if relay_user is None:
            return
        try:
            await self.service.call(
                relay_user, "fs/remove", {"path": join_path(root, folder)}
            )
        except WorkbenchError:
            pass
