"""请专家时选数据、钉住（SDD 0042 第五节第 4、6 项）。

用户按专家的数据需求声明逐样从本地知识库（A）或云端知识库（B）选：
- B：凭用户本人的令牌向 knowbase 核这一份在他的 knowbench 分区里，记下名字、类型、大小、摘要；
  选好的几份签一条这件委托的运行授权（只读这几份、钉这几个摘要）。
- A：经会合点服务流问本地 link：路径要在它报的本地知识库目录里（项目目录不算，项目里是产出），
  由 link 算整份的 sha256（`fs/digest`；老的 link 没有这个方法就一块块读回来在这里算）。
按声明核过（必填不缺、份数、格式、不重复）才钉；钉进委托在建委托的同一次提交里（Ledger）。
建委托没成就把刚签的授权交还。委托结束后由 runner 交还授权（GrantReleaser），用户就能删了。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.application.ports.workbench import CloudGrants, WorkbenchStore, WorkbenchStores
from app.application.workbench.agent_service import (
    MAX_READ_BYTES,
    AgentRefused,
    AgentService,
)
from app.domain.workbench.data_needs import (
    DataNeed,
    choice_problems,
    format_of,
    kind_of_name,
)
from app.domain.workbench.errors import (
    DataChoiceInvalid,
    DataRequired,
    UserDataUnavailable,
    WorkbenchError,
)

log = logging.getLogger(__name__)

CLOUD = "cloud"
LOCAL = "local"
MAX_FILE_BYTES = 50 * 1024 * 1024  # 与 knowbase 收的一致
# 委托的运行授权：knowbase 最长给 7 天；委托结束由 runner 交还，不等到期
GRANT_TTL_SECONDS = 7 * 24 * 3600

UserToken = Callable[[], Awaitable[str]]


@dataclass
class Chosen:
    """钉住前的一份：pin 是要写进 workbench_task_data 的那一行（grant_id 签了授权再补）。"""

    pin: dict[str, Any]
    format: str | None
    identity: str


@dataclass
class Prepared:
    pins: list[dict[str, Any]] = field(default_factory=list)
    grant_id: str | None = None


def choice_of(raw: dict[str, Any]) -> dict[str, Any]:
    """请求里的一份选择，规整成只留认得的字段（也是进幂等摘要的那一份）。"""
    if raw.get("source") == CLOUD:
        return {"need": raw.get("need"), "source": CLOUD, "item_id": raw.get("item_id")}
    return {"need": raw.get("need"), "source": LOCAL, "path": raw.get("path")}


def under(path: str, directory: str) -> bool:
    """path 在 directory 里（Windows 路径不分大小写、两种斜杠都认）。

    只做字面比较；`..`、链接、别名由本地 link 在读的时候挡。
    """
    windows = "\\" in directory or (len(directory) > 1 and directory[1] == ":")
    sep = "\\" if windows else "/"
    p = path.replace("/", "\\") if windows else path
    d = directory.replace("/", "\\") if windows else directory
    if windows:
        p, d = p.lower(), d.lower()
    d = d.rstrip(sep)
    return p.startswith(d + sep) and ".." not in p[len(d) :].split(sep)


class DataChooser:
    def __init__(self, *, cloud: CloudGrants, agent: AgentService | None) -> None:
        self.cloud = cloud
        self.agent = agent

    async def prepare(
        self,
        *,
        needs: list[DataNeed],
        choices: list[dict[str, Any]],
        relay_user: str | None,
        user_token: UserToken,
        ref: str,
    ) -> Prepared:
        """核用户选的、签授权，回要钉的那几行。没有声明的专家不收数据。"""
        if not needs:
            if choices:
                raise DataChoiceInvalid("this expert takes no data")
            return Prepared()
        if not choices:
            raise DataRequired("choose the data this expert needs first")
        token: str | None = None
        library: str | None = None
        chosen: list[Chosen] = []
        for position, raw in enumerate(choices):
            choice = choice_of(raw)
            if choice["source"] == CLOUD:
                if token is None:
                    token = await user_token()
                chosen.append(await self._cloud(token, choice, position))
            else:
                if library is None:
                    library = await self._library(relay_user)
                chosen.append(await self._local(relay_user, library, choice, position))
        problems = choice_problems(
            needs,
            [
                {"need": c.pin["slot"], "format": c.format, "identity": c.identity}
                for c in chosen
            ],
        )
        if problems:
            raise DataChoiceInvalid("; ".join(problems))
        cloud = [c.pin for c in chosen if c.pin["source"] == CLOUD]
        grant_id = None
        if cloud:
            assert token is not None
            grant_id = await self.cloud.grant(
                token,
                ref=ref,
                items=[{"item_id": p["item_id"], "sha256": p["sha256"]} for p in cloud],
                ttl_seconds=GRANT_TTL_SECONDS,
            )
            for pin in cloud:
                pin["grant_id"] = grant_id
        return Prepared(pins=[c.pin for c in chosen], grant_id=grant_id)

    async def release(self, prepared: Prepared) -> None:
        """建委托没成：刚签的授权交还（交还不成也不挡，授权到期自己失效）。"""
        if prepared.grant_id is None:
            return
        try:
            await self.cloud.release(prepared.grant_id)
        except WorkbenchError:
            log.warning("could not hand back an unused grant %s", prepared.grant_id)

    async def _cloud(self, token: str, choice: dict[str, Any], position: int) -> Chosen:
        item_id = choice.get("item_id")
        if not isinstance(item_id, str) or not item_id or len(item_id) > 64:
            raise DataChoiceInvalid("a cloud item is not named")
        item = await self.cloud.item(token, item_id)
        size = int(item.get("size_bytes") or 0)
        if size > MAX_FILE_BYTES:
            raise DataChoiceInvalid(f"{item.get('name')} is larger than 50 MB")
        media_type = str(item.get("media_type") or "")
        return Chosen(
            pin={
                "position": position,
                "slot": choice.get("need"),
                "source": CLOUD,
                "item_id": item_id,
                "path": None,
                "name": str(item.get("name") or item_id)[:255],
                "media_type": media_type,
                "size_bytes": size,
                "sha256": str(item.get("sha256") or ""),
            },
            format=format_of(media_type),
            identity=f"cloud:{item_id}",
        )

    async def _library(self, relay_user: str | None) -> str:
        if self.agent is None:
            raise UserDataUnavailable("the user's computer cannot be reached from here")
        if not relay_user:
            raise DataChoiceInvalid("this account has no computer paired")
        hello = await self._call(relay_user, "service/hello", None)
        library = hello.get("libraryDir")
        if not isinstance(library, str) or not library:
            raise DataChoiceInvalid(
                "no local knowledge base folder is set on the computer"
            )
        return library

    async def _local(
        self,
        relay_user: str | None,
        library: str,
        choice: dict[str, Any],
        position: int,
    ) -> Chosen:
        assert relay_user is not None
        path = choice.get("path")
        if not isinstance(path, str) or not path or len(path) > 1024:
            raise DataChoiceInvalid("a local file is not named")
        if not under(path, library):
            raise DataChoiceInvalid(
                "local data must come from the local knowledge base folder"
            )
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        kind = kind_of_name(name)
        size, sha256 = await self._digest(relay_user, path)
        if size > MAX_FILE_BYTES:
            raise DataChoiceInvalid(f"{name} is larger than 50 MB")
        return Chosen(
            pin={
                "position": position,
                "slot": choice.get("need"),
                "source": LOCAL,
                "item_id": None,
                "path": path,
                "name": name[:255],
                "media_type": kind[0] if kind else "application/octet-stream",
                "size_bytes": size,
                "sha256": sha256,
            },
            format=kind[1] if kind else None,
            identity=f"local:{path}",
        )

    async def _digest(self, relay_user: str, path: str) -> tuple[int, str]:
        try:
            got = await self._call(relay_user, "fs/digest", {"path": path})
            return int(got["size"]), str(got["sha256"])
        except AgentRefused as exc:
            if exc.code != "method_not_found":
                raise self._refused(exc) from None
        # 老的 link：一块块读回来算
        meta = await self._call(relay_user, "fs/getMetadata", {"path": path})
        if meta.get("type") != "file":
            raise DataChoiceInvalid("a chosen local path is not a file")
        size = int(meta.get("size") or 0)
        if size > MAX_FILE_BYTES:
            return size, ""
        digest = hashlib.sha256()
        offset = 0
        while offset < size:
            got = await self._call(
                relay_user,
                "fs/readFile",
                {"path": path, "offset": offset, "length": MAX_READ_BYTES},
            )
            piece = base64.b64decode(str(got.get("content") or ""), validate=True)
            if not piece or int(got.get("size") or 0) != size:
                raise DataChoiceInvalid("a chosen local file changed while it was read")
            digest.update(piece)
            offset += len(piece)
        return size, digest.hexdigest()

    async def _call(
        self, relay_user: str, method: str, params: dict[str, Any] | None
    ) -> dict[str, Any]:
        assert self.agent is not None
        for attempt in range(4):
            try:
                return await self.agent.call(relay_user, method, params)
            except AgentRefused as exc:
                if exc.code == "rate_limited" and attempt < 3:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                if exc.code == "method_not_found":
                    raise
                raise self._refused(exc) from None
        raise UserDataUnavailable("the user's computer is busy")  # pragma: no cover

    @staticmethod
    def _refused(exc: AgentRefused) -> WorkbenchError:
        if exc.code == "not_found":
            return DataChoiceInvalid("a chosen local file is not there")
        if exc.code in ("outside_whitelist", "local_denied"):
            return DataChoiceInvalid("the computer does not let this file be read")
        if exc.code == "too_large":
            return DataChoiceInvalid("a chosen local file is too large")
        return UserDataUnavailable("the user's computer refused to read the file")


def last_choices(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """上次钉住的几份，变成网页弹框里默认选上的样子。"""
    out = []
    for p in pins:
        item: dict[str, Any] = {
            "need": p["slot"],
            "source": p["source"],
            "name": p["name"],
        }
        if p["source"] == CLOUD:
            item["item_id"] = p["item_id"]
        else:
            item["path"] = p["path"]
        out.append(item)
    return out


class GrantReleaser:
    """委托结束后交还它的运行授权（runner 里一条线，隔一会儿扫一遍）。"""

    def __init__(
        self,
        stores: WorkbenchStores,
        cloud: CloudGrants,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self.stores = stores
        self.cloud = cloud
        self.interval_seconds = interval_seconds

    async def sweep(self) -> int:
        async with self.stores() as repo:
            found = await repo.grants_to_release()
        released = 0
        for grant_id in found:
            try:
                await self.cloud.release(grant_id)
            except WorkbenchError as exc:
                log.warning("grant release failed grant=%s code=%s", grant_id, exc.code)
                continue
            async with self.stores() as repo:
                await _mark(repo, grant_id)
            released += 1
        return released

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("grant release starting interval=%ss", self.interval_seconds)
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 — 这条线不能因为一次出错就停
                log.exception("grant release sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass


async def _mark(repo: WorkbenchStore, grant_id: str) -> None:
    async with repo.transaction():
        await repo.mark_grant_released(grant_id)
