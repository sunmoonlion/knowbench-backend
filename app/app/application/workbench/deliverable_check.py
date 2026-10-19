"""成稿「写了没有」的硬检查（0015 §3.3，0022 段 4）。

验收那一步说写了成稿文件，这一轮的 fileChange 事件里却没见到它（命令行写的、或模型只说没做）时，
云端经会合点服务流问代理：项目目录下那个文件在不在。在 → 认；明确不在 → 这一步验收不过；
问不到（没配通道、代理离线、没配对）→ 保持软检查，只记事件。验收时文件内容不取。

分身测试的每条用例跑完时，测试推进器经同一条路把这次写了的成稿读回来（SDD 0037），
放进这条用例的产出；读不到不影响结论。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, replace
from typing import Protocol

from app.application.ports.workbench import WorkbenchStores
from app.application.workbench.acceptance import Verdict
from app.application.workbench.agent_service import (
    MAX_READ_BYTES,
    AgentRefused,
    AgentService,
)
from app.domain.workbench.errors import WorkbenchError


class DeliverableTooLarge(WorkbenchError):
    """成稿比服务流一次能读的还大（MAX_READ_BYTES）。"""

    code = "deliverable_too_large"
    http_status = 413


class DeliverableCheck(Protocol):
    async def exists(
        self, *, owner: str, directory: str | None, path: str
    ) -> bool | None:
        """True 在、False 明确不在、None 问不到。"""
        ...


@dataclass(frozen=True)
class ReadFile:
    text: str
    mtime_ms: int | None  # 代理报的修改时间（毫秒）；没报就是不知道


class DeliverableReader(Protocol):
    async def read(
        self, *, owner: str, directory: str | None, path: str
    ) -> ReadFile | None:
        """成稿的文字与修改时间；问不到（没配对、没有项目目录）回 None；代理拒了、离线、太大抛 WorkbenchError。"""
        ...


def join_path(directory: str, relative: str) -> str:
    """项目目录是用户电脑上的绝对路径（Windows 反斜杠或 POSIX），成稿路径是项目内相对路径（正斜杠）。"""
    windows = re.match(r"^[A-Za-z]:\\", directory) is not None or directory.startswith(
        "\\\\"
    )
    sep = "\\" if windows else "/"
    tail = relative.replace("/", sep).lstrip(sep)
    return directory.rstrip(sep) + sep + tail


class AgentDeliverableCheck:
    def __init__(self, service: AgentService, stores: WorkbenchStores) -> None:
        self.service = service
        self.stores = stores

    async def exists(
        self, *, owner: str, directory: str | None, path: str
    ) -> bool | None:
        if not directory or not path:
            return None
        async with self.stores() as repo:
            identity = await repo.get_relay_identity(owner)
        relay_user = identity.get("relay_user") if identity else None
        if not relay_user:
            return None
        try:
            result = await self.service.call(
                str(relay_user), "fs/getMetadata", {"path": join_path(directory, path)}
            )
        except AgentRefused as exc:
            return False if exc.code == "not_found" else None
        except WorkbenchError:
            return None
        return result.get("type") == "file"

    async def read(
        self, *, owner: str, directory: str | None, path: str
    ) -> ReadFile | None:
        if not directory or not path:
            return None
        async with self.stores() as repo:
            identity = await repo.get_relay_identity(owner)
        relay_user = identity.get("relay_user") if identity else None
        if not relay_user:
            return None
        result = await self.service.call(
            str(relay_user),
            "fs/readFile",
            {
                "path": join_path(directory, path),
                "offset": 0,
                "length": MAX_READ_BYTES,
            },
        )
        size = int(result.get("size") or 0)
        if size > MAX_READ_BYTES:
            raise DeliverableTooLarge(
                "the deliverable is larger than one read", size=size
            )
        raw = base64.b64decode(str(result.get("content") or ""))
        mtime = result.get("mtime")
        return ReadFile(
            text=raw.decode("utf-8", errors="replace"),
            mtime_ms=int(mtime) if isinstance(mtime, int | float) else None,
        )


def harden(
    verdict: Verdict, *, written: bool, found: bool | None, output_file: str
) -> tuple[Verdict, bool, str]:
    """把软检查的结论和代理的回答合成最终结论：(验收结论, 文件在不在, 谁说的)。"""
    if written:
        return verdict, True, "events"
    if found is True:
        return verdict, True, "agent"
    if found is False:
        failed = replace(
            verdict,
            ok=False,
            failures=[*verdict.failures, f"成稿文件没有写到 {output_file}"],
        )
        return failed, False, "agent"
    return verdict, False, "events"
