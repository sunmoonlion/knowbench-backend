"""委托交付的成稿替用户写进 knowbase 产出区（SDD 0043 第 2.6 节「服务写入路」；runner 里一条线）。

委托成功时（`Advisor._finish`）每份成稿记一行待写，带交付那一刻取的 sha256；这条线隔一会儿
扫一遍（只扫已成功的委托）：经会合点读用户电脑上那份成稿的原样字节（同测试取成稿，SDD 0037），
以 knowbench 的服务身份（关系 `knowbase:user-data-output`）写进这位用户的产出区，带出处（哪位
专家哪一版）与这件委托的运行授权 ref（knowbase 按这个 ref 下签过的授权与读数留痕算升档上限，
独立审 10-30 两轮定），必需的数据缺了、用户选了照做的带「数据不全」（0043 第 3.6 节）。knowbase
在回包里回显认下的出处依据（`provenance_basis`），没有回显就当没写成、隔一阵再试（老的 knowbase
不认这几个参数，缺省不设上限，不能当写成了）。产出区的资料不参与解析，用户在 knowbase 里自己
决定要不要「转为数据」。

出处照实（0043 第 2.6 节）：读回来的那份和交付时取的指纹对不上（交付后在电脑上改过）就不写；
交付时没取到指纹的，link 报的修改时间不晚于交付才认，报不出来也不写；原因留着，委托页看得到。
写成专家的产出是假出处，替用户把改过的文件当自己的产出传上云也没问过本人。

写不进去不挡交付（委托早已交付，成稿也一直在用户的项目目录里）：电脑不在线、knowbase 暂时不通、
限流、令牌出错，记下原因、隔一阵再试，空间满了隔得更久（用户可能腾出地方），过了 7 天还没写进去
就停下；明确的内容问题（类型不对、内容和类型对不上、不是 UTF-8、太大、名字不行）、文件没了或
不在白名单里、会话没了，立即停下，原因留着。knowbase 说这次交付同名已经写过一份（409
`output_exists`，内容和这份不同）：查回那份，sha256 等于交付时的才记成已写，否则停下。测试的
委托不走这里：测试证据由 knowlab 写（L2）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStores
from app.application.workbench.deliverable_check import (
    DeliverableBytes,
    DeliverableTooLarge,
)
from app.domain.workbench.data_plan import short_of_required
from app.domain.workbench.errors import NotFound, WorkbenchError

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
# knowbase 说的内容问题：再试也不会成（knowbase user_data.py 的 kind_of、check_size、clean_name）
CONTENT_REFUSALS = frozenset(
    {
        "type_not_allowed",
        "content_mismatch",
        "not_utf8",
        "too_large",
        "name_invalid",
        "empty",
    }
)
# 空间满了：用户可能腾出地方，隔得久一点再试
QUOTA = "quota_exceeded"
# 代理说文件没了，再试也读不到；不在白名单里的（用户可能把目录加回来）照重试，7 天到期再停
GONE = frozenset({"not_found"})
# 修改时间推断（交付时没取到指纹）留的余量：电脑与云端的钟、写完到记账的间隔
MTIME_SLACK = timedelta(minutes=2)
# knowbase 产出名字的长度上限（user_data.clean_name）
MAX_NAME = 200


class OutputArea(Protocol):
    async def write_output(self, **kw: Any) -> dict[str, Any]: ...
    async def find_output(self, **kw: Any) -> dict[str, Any]: ...


class Retry(Exception):
    """这次没写成，过一阵再试。"""

    def __init__(self, why: str, *, wait: timedelta | None = None) -> None:
        super().__init__(why)
        self.wait = wait


class Refused(Exception):
    """不会再成了（内容问题、文件没了、出处对不上、账号或会话找不到）。"""


def task_ref(task_id: str) -> str:
    return f"knowbench:task:{task_id}"


def expert_of(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """请来的专家（knowmart 的分身）：profile_id 是 `knowbot:<专家>@<版本>`。内置的不记。"""
    profile = str(row.get("profile_id") or "")
    if row.get("pack_source") != "knowmart" or not profile.startswith(KNOWBOT):
        return None, None
    expert = profile[len(KNOWBOT) :].split("@", 1)[0] or None
    return expert, (str(row["profile_version"]) if row.get("profile_version") else None)


def output_name(path: str) -> str:
    """写进 knowbase 的名字带上项目里的相对路径：同一次交付不同目录里同名的成稿各是各的
    （knowbase 按任务、运行、名字幂等）。名字里不许有斜杠，段与段之间用全角「／」；超过
    knowbase 的上限就截掉后面、加一段路径的短哈希，扩展名留着（knowbase 按它认类型）。"""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    full = "／".join(parts)
    if len(full) <= MAX_NAME:
        return full
    stem, dot, ext = full.rpartition(".")
    if not dot or "／" in ext:
        stem, ext = full, ""
    tail = f"~{hashlib.sha256(path.encode()).hexdigest()[:8]}" + (
        f".{ext}" if ext else ""
    )
    return stem[: MAX_NAME - len(tail)] + tail


class OutputWriter:
    def __init__(
        self,
        stores: WorkbenchStores,
        reader: DeliverableBytes,
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
                await self._later(
                    row, why, wait=exc.wait if isinstance(exc, Retry) else None
                )
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
            try:
                session: dict[str, Any] | None = await repo.get_session(
                    str(row["session_id"])
                )
            except NotFound:
                session = None
            resolution = await repo.get_task_resolution(task_id) or {}
        if subject is None:
            raise Refused("找不到委托人的账号")
        if session is None:
            raise Refused("这件委托的会话没了")
        accepted = row.get("accepted_sha256")
        try:
            found = await self.reader.read_bytes(
                owner=owner,
                directory=session.get("project_root"),
                path=str(row["path"]),
            )
        except DeliverableTooLarge as exc:
            raise Refused(exc.code) from exc
        except WorkbenchError as exc:
            if exc.code in GONE:
                raise Refused(f"成稿读不到了：{exc.code}") from exc
            raise Retry(f"成稿没读到：{exc.code}") from exc
        if found is None:
            raise Retry("成稿没读到：问不到那台电脑")
        _check_delivered(row, accepted, found.content, found.mtime_ms)
        expert, version = expert_of(row)
        where = {
            "beneficiary": subject,
            "task_ref": task_ref(task_id),
            "run_ref": str(row["run_ref"]),
            "name": output_name(str(row["path"])),
        }
        grant_ref = resolution.get("grant_ref")
        started, finished = _task_window(row)
        try:
            written = await self.area.write_output(
                **where,
                content=found.content,
                expert_id=expert,
                expert_version=version,
                # 用了哪些输入由 knowbase 按这件委托的授权 ref 算（ref 下签过的授权与读数
                # 留痕）；没有清单的委托（内置专家）不带，knowbase 缺省按「只许本人用」
                grant_ref=grant_ref,
                # 这件委托的起止（内置专家没有 ref 也带）：knowbase 把这段时间里受益人经
                # knowbench 会话授权读过的资料也算进上限。工作台 MCP 的令牌只绑用户与沙箱，
                # 分不出调用来自对话还是委托步骤，委托里的模型可能借同一项目别的对话读了
                # 数据，按 ref 算会漏；不改令牌与平台，就按时间窗宁严勿松（独立审 10-30）
                task_started_at=started,
                task_finished_at=finished,
                incomplete=short_of_required(
                    (resolution.get("detail") or {}).get("gaps") or []
                ),
            )
        except Exception as exc:
            code = _code(exc)
            if _status(exc) == 409 and code == "output_exists":
                return await self._already_written(row, where, accepted)
            if code == QUOTA:
                raise Retry(f"knowbase 空间满了：{code}", wait=RETRY[-1]) from exc
            if code in CONTENT_REFUSALS:
                raise Refused(f"knowbase 不收：{code}") from exc
            raise
        if not _basis_echoed(written, grant_ref, started, finished):
            # 老的 knowbase 不认出处依据、缺省不设上限：不能当写成了
            raise Retry("knowbase 没回显认下的出处依据")
        return written

    async def _already_written(
        self, row: dict[str, Any], where: dict[str, Any], accepted: str | None
    ) -> dict[str, Any]:
        """409 output_exists：这次交付同名已经有一份，内容和这份不同。查回来，sha256 等于
        交付时取的才记成已写（上次写成了、没来得及记）；等于这次交付另一份成稿的，是名字
        撞了；都不是就停下（那份不是交付的）。"""
        found = await self.area.find_output(**where)
        sha = str(found.get("sha256") or "")
        if accepted and sha == accepted:
            return found
        async with self.stores() as repo:
            others = await repo.list_task_outputs(str(row["task_id"]))
        if any(
            o.get("accepted_sha256") == sha and o["path"] != row["path"] for o in others
        ):
            raise Refused("和这次交付的另一份成稿重名，knowbase 里那个名字已经是那一份")
        raise Refused("knowbase 里这次交付同名的是另一份，不是交付的那份")

    async def _later(
        self, row: dict[str, Any], why: str, *, wait: timedelta | None = None
    ) -> None:
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
            next_try_at=self.now() + (wait or RETRY[min(attempts, len(RETRY)) - 1]),
        )

    async def _settle(self, row: dict[str, Any], **fields: Any) -> None:
        if fields.get("last_error"):
            fields["last_error"] = str(fields["last_error"])[:200]
        async with self.stores() as repo:
            async with repo.transaction():
                await repo.update_task_output(str(row["id"]), **fields)


def _check_delivered(
    row: dict[str, Any], accepted: str | None, content: bytes, mtime_ms: int | None
) -> None:
    """是不是交付的那份：有交付时的指纹就比 sha256；没有的，link 报的修改时间不晚于交付
    （记这一行的时候）才认；都核不了就不写。"""
    if accepted:
        if hashlib.sha256(content).hexdigest() != accepted:
            raise Refused("成稿交付后改过，和交付时的那份对不上，没写")
        return
    delivered = row.get("created_at")
    if mtime_ms is None or not isinstance(delivered, datetime):
        raise Refused(
            "交付时没取到成稿的指纹，电脑也没报修改时间，核不了是不是交付的那份"
        )
    # 按修改时间推断：留 2 分钟余量（两边的钟、写完到记账的间隔）
    if datetime.fromtimestamp(mtime_ms / 1000, UTC) > delivered + MTIME_SLACK:
        raise Refused(
            "按修改时间推断：成稿在交付之后改过（交付时没取到指纹，修改时间晚于交付），没写"
        )


def _task_window(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """委托的起止（ISO8601）：起是建委托的时候；止取交付记这一行与委托最后一次更新里晚的
    那个（时间窗宽一点只会更严）。"""
    started = row.get("task_created_at")
    ends = [
        t
        for t in (row.get("task_updated_at"), row.get("created_at"))
        if isinstance(t, datetime)
    ]
    return (
        started.isoformat() if isinstance(started, datetime) else None,
        max(ends).isoformat() if ends else None,
    )


def _basis_echoed(
    written: dict[str, Any],
    grant_ref: str | None,
    started: str | None,
    finished: str | None,
) -> bool:
    """knowbase 回显的出处依据（`provenance_basis`）和我们给的对得上：ref（给了的话）与
    委托起止 `task_window: {started_at, finished_at}`（按时刻比，不按写法）。没有这一栏、
    或对不上，就是 knowbase 没认这几个参数。"""
    basis = written.get("provenance_basis") if isinstance(written, dict) else None
    if not isinstance(basis, dict):
        return False
    if grant_ref and basis.get("grant_ref") != grant_ref:
        return False
    window = basis.get("task_window")
    if not isinstance(window, dict):
        return False
    return _same_time(window.get("started_at"), started) and _same_time(
        window.get("finished_at"), finished
    )


def _same_time(echoed: object, sent: str | None) -> bool:
    if sent is None:
        return echoed is None
    try:
        return datetime.fromisoformat(str(echoed)) == datetime.fromisoformat(sent)
    except ValueError:
        return False


def _code(exc: Exception) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__)


def _status(exc: Exception) -> int:
    return int(getattr(exc, "status", 0) or 0)
