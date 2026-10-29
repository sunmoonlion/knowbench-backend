"""委托交付的成稿替用户写进 knowbase 产出区（SDD 0043 第 2.6 节「服务写入路」；runner 里一条线）。

委托成功时（`Advisor._finish`）每份成稿记一行待写；这条线隔一会儿扫一遍：经会合点读用户电脑上
那份成稿（同测试取成稿，SDD 0037），以 knowbench 的服务身份（关系 `knowbase:user-data-output`）
写进这位用户的产出区，带出处（哪位专家哪一版、用了哪组输入）。产出区的资料不参与解析，用户在
knowbase 里自己决定要不要「转为数据」。

写不进去不挡交付（委托早已交付，成稿也一直在用户的项目目录里）：电脑不在线、knowbase 暂时不通，
记下原因、隔一阵再试，过了 7 天还没写进去就停下；knowbase 明说不收（类型不对、空间满了、
这次交付已经写过另一份）也停下，原因留着。测试的委托不走这里：测试证据由 knowlab 写（L2）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStores
from app.application.workbench.deliverable_check import (
    DeliverableReader,
    DeliverableTooLarge,
)
from app.domain.workbench.errors import WorkbenchError

log = logging.getLogger(__name__)

# 隔多久再试：逐次拉长，最长 6 小时；过了 GIVE_UP 还没写进去就停下
RETRY = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)
GIVE_UP = timedelta(days=7)
KNOWBOT = "knowbot:"


class OutputArea(Protocol):
    async def grant_input_set(self, grant_id: str) -> str | None: ...
    async def write_output(self, **kw: Any) -> dict[str, Any]: ...


class Retry(Exception):
    """这次没写成，过一阵再试。"""


class Refused(Exception):
    """不会再成了（knowbase 明说不收、成稿太大、账号找不到）。"""


def task_ref(task_id: str) -> str:
    return f"knowbench:task:{task_id}"


def expert_of(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """请来的专家（knowmart 的分身）：profile_id 是 `knowbot:<专家>@<版本>`。内置的不记。"""
    profile = str(row.get("profile_id") or "")
    if row.get("pack_source") != "knowmart" or not profile.startswith(KNOWBOT):
        return None, None
    expert = profile[len(KNOWBOT) :].split("@", 1)[0] or None
    return expert, (str(row["profile_version"]) if row.get("profile_version") else None)


def file_name(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


class OutputWriter:
    def __init__(
        self,
        stores: WorkbenchStores,
        reader: DeliverableReader,
        area: OutputArea,
        *,
        interval_seconds: float = 30.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.stores = stores
        self.reader = reader
        self.area = area
        self.interval_seconds = interval_seconds
        self.now = now

    async def sweep(self) -> int:
        async with self.stores() as repo:
            due = await repo.due_task_outputs(self.now())
        written = 0
        for row in due:
            try:
                found = await self._write(row)
            except Refused as exc:
                log.warning("output refused task=%s why=%s", row["task_id"], exc)
                await self._settle(row, status="failed", last_error=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 — 一份出错不挡别的，过一阵再试
                why = str(exc) if isinstance(exc, Retry) else _code(exc)
                log.warning("output not written task=%s why=%s", row["task_id"], why)
                await self._later(row, why)
                continue
            await self._settle(
                row,
                status="written",
                last_error=None,
                material_id=str(found["material_id"]),
                version_no=int(found["version_no"]),
                sha256=str(found.get("sha256") or "") or None,
                written_at=self.now(),
            )
            written += 1
        return written

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("output writer starting interval=%ss", self.interval_seconds)
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 — 这条线不能因为一次出错就停
                log.exception("output writer sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    async def _write(self, row: dict[str, Any]) -> dict[str, Any]:
        task_id = str(row["task_id"])
        owner = str(row["owner_actor_id"])
        async with self.stores() as repo:
            subject = await repo.subject_of_actor(owner)
            session = await repo.get_session(str(row["session_id"]))
            grants = {
                str(d["grant_id"])
                for d in await repo.list_task_data(task_id)
                if d.get("grant_id")
            }
        if subject is None:
            raise Refused("找不到委托人的账号")
        try:
            found = await self.reader.read(
                owner=owner,
                directory=session.get("project_root"),
                path=str(row["path"]),
            )
        except DeliverableTooLarge as exc:
            raise Refused(exc.code) from exc
        except WorkbenchError as exc:
            raise Retry(f"成稿没读到：{exc.code}") from exc
        if found is None:
            raise Retry("成稿没读到：问不到那台电脑")
        expert, version = expert_of(row)
        # 用了哪组输入（knowbase 转为数据时据此限升档）：钉的数据都出自一条运行授权才记得准；
        # 分几次签的（等数据后续签）有几组，产出只能记一组，先不记
        input_set = None
        if len(grants) == 1:
            try:
                input_set = await self.area.grant_input_set(next(iter(grants)))
            except Exception as exc:  # noqa: BLE001
                if not _refused(exc):
                    raise
        try:
            return await self.area.write_output(
                beneficiary=subject,
                task_ref=task_ref(task_id),
                run_ref=str(row["run_ref"]),
                name=file_name(str(row["path"])),
                content=found.text.encode("utf-8"),
                expert_id=expert,
                expert_version=version,
                input_set_id=input_set,
            )
        except Exception as exc:
            if _refused(exc):
                raise Refused(f"knowbase 不收：{_code(exc)}") from exc
            raise

    async def _later(self, row: dict[str, Any], why: str) -> None:
        attempts = int(row["attempts"]) + 1
        created = row.get("created_at")
        if isinstance(created, datetime) and self.now() - created >= GIVE_UP:
            await self._settle(
                row,
                status="failed",
                attempts=attempts,
                last_error=f"过了 7 天还没写进去：{why}"[:200],
            )
            return
        await self._settle(
            row,
            attempts=attempts,
            last_error=why[:200],
            next_try_at=self.now() + RETRY[min(attempts, len(RETRY)) - 1],
        )

    async def _settle(self, row: dict[str, Any], **fields: Any) -> None:
        if fields.get("last_error"):
            fields["last_error"] = str(fields["last_error"])[:200]
        async with self.stores() as repo:
            async with repo.transaction():
                await repo.update_task_output(str(row["id"]), **fields)


def _code(exc: Exception) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__)


def _refused(exc: Exception) -> bool:
    return 400 <= int(getattr(exc, "status", 0) or 0) < 500
