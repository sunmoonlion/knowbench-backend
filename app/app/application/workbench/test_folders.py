"""分身测试的工作目录（账 129，所有者 2026-10-18「按你建议」）。

每次测试在测试电脑上用自己的目录 `<工作区>/runs/<受理时间>-<测试编号>`，开跑前经会合点服务流请代理建好，
测完就删；开跑前顺手把 runs 下别的目录也删掉（一次只跑一个，别的都是已经结束的）。这样新测试开跑时，
测试电脑上没有别人的产出可读——Codex 的沙箱只限写不限读，靠这个隔开。排查用的交回物、验收结果、
过程记录在工作台数据库里，照旧留 7 天。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStores
from app.application.workbench.agent_service import AgentRefused, AgentService
from app.application.workbench.deliverable_check import join_path
from app.domain.workbench.errors import WorkbenchError

log = logging.getLogger(__name__)

RUNS = "runs"
TMP = ".tmp"  # runs 下给测试放临时文件的目录（测试电脑的 TMPDIR 指到这里）
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def run_folder(run: dict[str, Any]) -> str:
    """这次测试的目录（相对工作区）。受理时间在前，按名字排就是按先后。"""
    stamp = run["created_at"].strftime("%Y%m%d%H%M%S")
    return f"{RUNS}/{stamp}-{_UNSAFE.sub('-', str(run['run_id']))[:80]}"


class FolderRefused(WorkbenchError):
    """代理明确拒了删或建（不是离线、超时这类过一会儿就好的）：等下去也不会好，
    立刻报错，写明是哪个目录（审读第 15 条）。"""

    code = "test_folder_refused"


class TestFolders(Protocol):
    async def prepare(self, *, owner: str, root: str, folder: str) -> bool:
        """删掉 runs 下别的目录，建好这一个。建好了回 True；问不到回 False（下一轮再试）；
        代理拒了删或建抛 FolderRefused。"""
        ...

    async def remove(self, *, owner: str, root: str, folder: str) -> None:
        """测完删掉；删不掉不报错，下次开跑前还会再删。"""
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
        runs, mine = join_path(root, RUNS), folder.split("/", 1)[1]
        try:
            listed = await self.service.call(
                relay_user, "fs/readDirectory", {"path": runs}
            )
        except AgentRefused as exc:
            if exc.code != "not_found":
                return False
            listed = {"entries": []}
        except WorkbenchError:
            return False
        try:
            # 本地 link 服务流 fs/readDirectory 的条目是 {name, type: dir|file|link|other, …}（sunmoon-link service.ts）
            for entry in listed.get("entries") or []:
                name = str(entry.get("name") or "")
                if name and name != mine and entry.get("type") == "dir":
                    old = join_path(runs, name)
                    try:
                        await self.service.call(relay_user, "fs/remove", {"path": old})
                    except AgentRefused as exc:
                        # 上一次留下删不掉的（比如只读目录）：记一行，不挡这一次；
                        # 一次删不掉就让以后所有测试都开不了跑更糟（Fable 评审 P1-3）
                        log.warning("old test folder not removed %s: %s", old, exc.code)
        except WorkbenchError:
            return False
        target = join_path(root, folder)
        try:
            await self.service.call(
                relay_user, "fs/createDirectory", {"path": target, "recursive": True}
            )
            # 临时文件的目录（测试电脑的 TMPDIR）：上面随别的目录清掉了，这里再建一个空的
            # （Fable 评审 P2-14）
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
