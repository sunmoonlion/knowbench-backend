"""成稿「写了没有」的硬检查（0015 §3.3，0022 段 4）。

验收那一步说写了成稿文件，这一轮的 fileChange 事件里却没见到它（命令行写的、或模型只说没做）时，
云端经会合点服务流问代理：项目目录下那个文件在不在。在 → 认；明确不在 → 这一步验收不过；
问不到（没配通道、代理离线、没配对）→ 保持软检查，只记事件。验收时文件内容不取。

分身测试的每条用例跑完时，测试推进器经同一条路把这次写了的成稿读回来（SDD 0037），
放进这条用例的产出；读不到不影响结论。

验收认下成稿时还问一下那份文件的 sha256（link 的 `fs/digest`，在用户电脑上算整份，不经会合点
搬文件；老的 link 没有这一问就读回来算）。交付后替用户写进 knowbase 产出区之前，按原始字节
（`read_bytes`，不转文字）再算一遍比对：对不上就是交付后改过（0043 第 2.6 节「出处照实」）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, replace
from typing import Protocol

from app.application.ports.workbench import WorkbenchStores
from app.application.workbench.acceptance import Verdict
from app.application.workbench.agent_service import (
    MAX_READ_BYTES,
    AgentRefused,
    AgentService,
    AgentServiceFailed,
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


@dataclass(frozen=True)
class RawFile:
    content: bytes  # 原样的字节：写进 knowbase 用，不转文字
    mtime_ms: int | None


class DeliverableReader(Protocol):
    async def read(
        self, *, owner: str, directory: str | None, path: str
    ) -> ReadFile | None:
        """成稿的文字与修改时间；问不到（没配对、没有项目目录）回 None；代理拒了、离线、太大抛 WorkbenchError。"""
        ...


class DeliverableBytes(Protocol):
    async def read_bytes(
        self, *, owner: str, directory: str | None, path: str
    ) -> RawFile | None:
        """成稿原样的字节（同 read 的规矩）：二进制、非 UTF-8 的不改一个字节。"""
        ...


class DeliverableDigest(Protocol):
    async def digest(
        self, *, owner: str, directory: str | None, path: str
    ) -> str | None:
        """那份文件的 sha256；问不到回 None（不挡验收）。"""
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
        found = await self.read_bytes(owner=owner, directory=directory, path=path)
        if found is None:
            return None
        return ReadFile(
            text=found.content.decode("utf-8", errors="replace"),
            mtime_ms=found.mtime_ms,
        )

    async def digest(
        self, *, owner: str, directory: str | None, path: str
    ) -> str | None:
        relay_user = await self._relay_user(owner) if directory and path else None
        if not relay_user or not directory:
            return None
        try:
            result = await self.service.call(
                relay_user, "fs/digest", {"path": join_path(directory, path)}
            )
        except AgentRefused as exc:
            if exc.code != "method_not_found":
                return None
            # 老的 link 没有 fs/digest：读回来算（成稿一次读得完的才行）
            try:
                found = await self.read_bytes(
                    owner=owner, directory=directory, path=path
                )
            except WorkbenchError:
                return None
            return hashlib.sha256(found.content).hexdigest() if found else None
        except WorkbenchError:
            return None
        sha = str(result.get("sha256") or "")
        return sha if re.fullmatch(r"[0-9a-f]{64}", sha) else None

    async def _relay_user(self, owner: str) -> str | None:
        async with self.stores() as repo:
            identity = await repo.get_relay_identity(owner)
        relay_user = identity.get("relay_user") if identity else None
        return str(relay_user) if relay_user else None

    async def read_bytes(
        self, *, owner: str, directory: str | None, path: str
    ) -> RawFile | None:
        if not directory or not path:
            return None
        relay_user = await self._relay_user(owner)
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
        size = result.get("size")
        if type(size) is not int or size < 0:
            raise AgentServiceFailed("invalid deliverable size")
        if size > MAX_READ_BYTES:
            raise DeliverableTooLarge(
                "the deliverable is larger than one read", size=size
            )
        content = result.get("content")
        if not isinstance(content, str):
            raise AgentServiceFailed("invalid deliverable content")
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AgentServiceFailed("invalid deliverable encoding") from exc
        # This is a whole-file read at offset zero. A partial response must not
        # become an evidence copy or the old-link digest of a complete report.
        # Older responses may omit eof; their byte length must still match.
        if len(raw) != size or ("eof" in result and result["eof"] is not True):
            raise AgentServiceFailed("incomplete deliverable read")
        mtime = result.get("mtime")
        return RawFile(
            content=raw,
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
