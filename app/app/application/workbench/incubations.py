"""作为运行方跑孵化的一轮（专家孵化方案第八节，契约 knowbot-incubation/v1，knowlab 定）。

作者在 knowlab 点「AI 起草」或「让 AI 改…」，knowlab 经服务关系 `knowbench:incubate` 把这一轮
交过来。都在作者（`owner_subject`）的账号下跑（decisions ⑮⑯：钱是作者的）：他接入的电脑、
他选的工作区根目录、他自己的沙箱与模型 key（`runner`，形状同测试）。在那个根目录下建固定的
「孵化」子目录（经会合点服务流请他的本地 link 建，`projects/folder`）与同名项目，每一轮在里面开
一段新的专家对话，用内置的孵化方法（`packs.INCUBATE`，单步，不在请专家列表里）建一件委托，
runner 照常驱动。作者在自己的工作台里看得到这件委托和花了多少钱。

交给模型的只有契约列死的材料（`incubations.material`，0029 第 6.4 节），不读作者数据的内容。
交回物先过现有的验收种类（结构：四样都在、每项该有的键、种类与改法只取认得的值），过不了重做
一次；过了再照契约逐条核（建议只碰方法层的格子、出处按种类引……），不合的这一轮记失败。费用
上限是请求里的（作者的钱）：到了就停，记 over_budget。同一台电脑、同一个根目录一次只跑一轮，
其余按受理先后排队（同测试，账 129）。电脑不在线、「孵化」目录还没建好，就停在 waiting
（「等你接入电脑」），过了这一轮的时限还不行就报错结束。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import ValidationError

from app.application.ports.workbench import WorkbenchStore, WorkbenchStores
from app.application.workbench.agent_service import AgentRefused, AgentService
from app.application.workbench.deliverable_check import join_path
from app.application.workbench.interactions import cancel_task
from app.application.workbench.ledger import Ledger
from app.application.workbench.project_service import ONLINE
from app.application.workbench.session_service import SessionService
from app.application.workbench.test_folders import FolderRefused
from app.application.workbench.test_runs import queued_behind
from app.domain.workbench.errors import (
    IncubationConflict,
    IncubationNoComputer,
    NotFound,
    WorkbenchError,
)
from app.domain.workbench.incubations import (
    CONTRACT,
    FINAL,
    Handback,
    IncubationRequest,
    label,
    material,
    task_key,
)
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.packs import INCUBATE
from app.domain.workbench.states import TASK_TERMINAL, TaskState, WaitingReason

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
FOLDER = "孵化"  # 作者选的根目录下的固定子目录，也是项目名（第 8.2 节第 3 条）
ERROR_CHARS = 1000


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _money(value: Any) -> str:
    return str(Decimal(str(value or "0")).quantize(Decimal("0.000001")))


def place(row: dict[str, Any]) -> tuple[str, str, str]:
    """这一轮在哪跑：谁、哪台电脑、哪个根目录。同一处一次只跑一轮。"""
    request = row.get("request") or {}
    runner = request.get("runner") or {}
    return (
        str(request.get("owner_subject") or ""),
        str(runner.get("environment_id") or ""),
        str(runner.get("workspace_root") or ""),
    )


async def authors_computer(
    repo: WorkbenchStore, request: IncubationRequest
) -> tuple[str, dict[str, Any]]:
    """作者的账号与他选的那台电脑：他登录过工作台，那台电脑是他接入的、那个根目录在它的
    白名单里。不是就抛 IncubationNoComputer（同测试，decisions ⑯）。"""
    runner = request.runner
    actor = await repo.actor_for_subject(request.owner_subject)
    if actor is None:
        raise IncubationNoComputer("this user has never signed in to the workbench")
    env = next(
        (
            e
            for e in await repo.list_environments(owner_actor_id=actor)
            if str(e["id"]) == runner.environment_id
            and runner.workspace_root in list(e.get("roots") or [])
        ),
        None,
    )
    if env is None:
        raise IncubationNoComputer("that computer or workspace root is not the user's")
    return actor, env


class IncubationService:
    """契约的三个接口：受理、查、取消。真正往前推的是 IncubationConductor。"""

    def __init__(
        self,
        repo: WorkbenchStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.now = now

    async def accept(
        self, request: IncubationRequest, *, caller: str
    ) -> dict[str, Any]:
        await authors_computer(self.repo, request)
        prior = await self.repo.get_incubation(request.incubation_id)
        if prior is not None:
            if prior["digest"] != request.digest():
                raise IncubationConflict(
                    "this incubation id was submitted with other content",
                    incubation_id=request.incubation_id,
                )
            return await self.status(request.incubation_id)
        async with self.repo.transaction():
            await self.repo.insert_incubation(
                {
                    "incubation_id": request.incubation_id,
                    "caller": caller,
                    "owner_subject": request.owner_subject,
                    "digest": request.digest(),
                    "request": request.model_dump(mode="json"),
                    "budget_limit": Decimal(request.budget.limit),
                    "timeout_seconds": request.timeout_seconds,
                }
            )
        return await self.status(request.incubation_id)

    async def status(self, incubation_id: str) -> dict[str, Any]:
        row = await self.repo.get_incubation(incubation_id)
        if row is None:
            raise NotFound("no such incubation", incubation_id=incubation_id)
        return await view(self.repo, row)

    async def cancel(self, incubation_id: str) -> dict[str, Any]:
        """作者在 knowlab 点了取消：在跑的委托停下（同作者在工作台里停）；已经结束的回原状态。"""
        async with self.repo.transaction():
            row = await self.repo.get_incubation(incubation_id, for_update=True)
            if row is None:
                raise NotFound("no such incubation", incubation_id=incubation_id)
            if row["status"] in FINAL:
                return await view(self.repo, row)
            spent = Decimal(str(row["spent"]))
            task = (
                await self.repo.get_task(str(row["task_id"]))
                if row.get("task_id")
                else None
            )
            if task is not None:
                spent = _used(task)
            await self.repo.update_incubation(
                incubation_id,
                status="cancelled",
                finished_at=self.now(),
                waiting_for=None,
                spent=spent,
            )
        if task is not None and TaskState(task["state"]) not in TASK_TERMINAL:
            await cancel_task(
                self.repo, str(task["id"]), owner_actor_id=str(task["owner_actor_id"])
            )
        log.info("incubation cancelled id=%s", incubation_id)
        found = await self.repo.get_incubation(incubation_id)
        assert found is not None
        return await view(self.repo, found)


def _used(task: dict[str, Any]) -> Decimal:
    return Decimal(str((task.get("budget") or {}).get("used") or "0"))


async def view(repo: WorkbenchStore, row: dict[str, Any]) -> dict[str, Any]:
    """一轮的状态与结果（契约的 result）：没结论时花费算到此刻；waiting 写明等什么；
    排在作者别的委托后面算 queued；只有 succeeded 带交回物。"""
    status = row["status"]
    spent: Any = row["spent"]
    task = None
    if status not in FINAL and row.get("task_id"):
        task = await repo.get_task(str(row["task_id"]))
        spent = _used(task)
    if status not in FINAL and row.get("waiting_for"):
        status = "waiting"
    elif status == "running" and task is not None and queued_behind(task):
        status = "queued"
    return {
        "contract": CONTRACT,
        "incubation_id": row["incubation_id"],
        "status": status,
        "cost": {"spent": _money(spent)},
        "error": row.get("error"),
        "task_id": str(row["task_id"]) if row.get("task_id") else None,
        "waiting_for": row.get("waiting_for") if status == "waiting" else None,
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "handback": row.get("handback") if status == "succeeded" else None,
    }


class IncubationFolders(Protocol):
    async def ensure(self, *, owner: str, root: str, folder: str) -> bool:
        """在作者那台电脑的根目录下建好（或认出已有的）这个子目录。建好了回 True；问不到回
        False（下一轮再试）；本地 link 明确拒了抛 FolderRefused。"""
        ...


class AgentIncubationFolders:
    """经会合点服务流请作者的本地 link 建「孵化」目录（`projects/folder`，账 165 的新建项目；
    已有就认出来，不清里面的东西）。"""

    def __init__(self, service: AgentService, stores: WorkbenchStores) -> None:
        self.service = service
        self.stores = stores

    async def ensure(self, *, owner: str, root: str, folder: str) -> bool:
        async with self.stores() as repo:
            identity = await repo.get_relay_identity(owner)
        relay_user = identity.get("relay_user") if identity else None
        if not relay_user:
            return False
        target = join_path(root, folder)
        try:
            await self.service.call(
                str(relay_user), "projects/folder", {"path": target}
            )
        except AgentRefused as exc:
            raise FolderRefused(f"{target}: {exc.code}") from None
        except WorkbenchError:
            return False
        return True


class IncubationConductor:
    """把在跑的孵化往前推一步：建目录与项目、开对话与委托、跟着委托、收交回物、超预算与超时收尾。"""

    def __init__(
        self,
        stores: WorkbenchStores,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        folders: IncubationFolders | None = None,
    ) -> None:
        self.stores = stores
        self.now = now
        # 没配会合点服务流时为 None：不建目录，项目照样记在根目录下的「孵化」（本地开发、测试）
        self.folders = folders

    async def tick(self) -> int:
        async with self.stores() as repo:
            rows = await repo.list_incubations(statuses=ACTIVE)
        # 已经开跑的接着推；同一台电脑、同一个根目录一次只开一轮，其余按受理先后排队
        busy = {place(r) for r in rows if r["started_at"] is not None}
        chosen = [r for r in rows if r["started_at"] is not None]
        for row in rows:
            if row["started_at"] is None and place(row) not in busy:
                busy.add(place(row))
                chosen.append(row)
        moved = 0
        for row in chosen:
            try:
                await self.advance(row["incubation_id"])
                moved += 1
            except Exception:  # noqa: BLE001 - 一轮出错不拖累别的
                log.exception("incubation advance failed id=%s", row["incubation_id"])
        return moved

    async def advance(self, incubation_id: str) -> None:
        async with self.stores() as repo:
            row = await repo.get_incubation(incubation_id)
            if row is None or row["status"] not in ACTIVE:
                return
            request = IncubationRequest.model_validate(row["request"])
            if row["started_at"] is None:
                await self._start(repo, row, request)
            else:
                await self._follow(repo, row)

    async def _start(
        self, repo: WorkbenchStore, row: dict[str, Any], request: IncubationRequest
    ) -> None:
        now = self.now()
        try:
            actor, env = await authors_computer(repo, request)
        except IncubationNoComputer as exc:
            await self._finish(repo, row, status="failed", error=exc.message)
            return
        waited = (now - row["created_at"]).total_seconds()
        root = request.runner.workspace_root
        if env.get("status") != ONLINE:
            # 那台电脑不在线不开跑，免得委托落到他名下别的电脑上（同测试，账 133）
            await self._wait(repo, row, "computer_offline")
            if waited > row["timeout_seconds"]:
                await self._finish(
                    repo, row, status="failed", error="the chosen computer is offline"
                )
            return
        try:
            ready = self.folders is None or await self.folders.ensure(
                owner=actor, root=root, folder=FOLDER
            )
        except FolderRefused as exc:
            await self._finish(
                repo,
                row,
                status="failed",
                error="the computer refused to make the folder: " + exc.message,
            )
            return
        if not ready:
            await self._wait(repo, row, "folder_not_ready")
            if waited > row["timeout_seconds"]:
                await self._finish(
                    repo,
                    row,
                    status="failed",
                    error="the computer could not make the folder",
                )
            return
        project = await repo.find_project(
            owner_actor_id=actor,
            environment_id=str(env["id"]),
            workspace_root=root,
            path=FOLDER,
        )
        if project is None:
            async with repo.transaction():
                project_id = await repo.create_project(
                    owner_actor_id=actor,
                    environment_id=str(env["id"]),
                    workspace_root=root,
                    path=FOLDER,
                    title=FOLDER,
                )
        else:
            project_id = str(project["id"])
        # 取消可能刚到：开委托前再看一眼，已经不是排队中就不开
        fresh = await repo.get_incubation(row["incubation_id"])
        if fresh is None or fresh["status"] != "queued":
            return
        try:
            started = await SessionService(repo).start(
                owner_actor_id=actor,
                kind="expert",
                project_id=project_id,
                title=label(request),
            )
            made = await Ledger(repo).handover(
                HandoverRequest(
                    idempotency_key=task_key(request.incubation_id),
                    session_id=started["session_id"],
                    profile_id=INCUBATE.profile_id,
                    original_input={
                        "text": label(request),
                        # 契约列死的材料：模型只看这些（0029 第 6.4 节）
                        "incubation": material(request),
                    },
                    budget_limit=Decimal(request.budget.limit),
                    client_context={
                        "entry": "knowlab-incubation",
                        "incubation_id": request.incubation_id,
                    },
                ),
                owner_actor_id=actor,
            )
        except WorkbenchError as exc:
            # 没有沙箱（没填模型 key）这类：说清缺什么，knowlab 在 web端提示作者
            await self._finish(repo, row, status="failed", error=exc.message)
            return
        session = await repo.get_session(started["session_id"])
        async with repo.transaction():
            if not made.get("queued_behind"):
                await repo.enqueue_command(
                    session_id=started["session_id"],
                    sandbox_id=str(session["sandbox_id"]),
                    kind="task.drive",
                    payload={"task_id": made["task_id"]},
                )
            await repo.update_incubation(
                row["incubation_id"],
                status="running",
                started_at=now,
                waiting_for=None,
                project_id=project_id,
                session_id=started["session_id"],
                task_id=made["task_id"],
            )
        log.info(
            "incubation started id=%s task=%s", row["incubation_id"], made["task_id"]
        )

    async def _follow(self, repo: WorkbenchStore, row: dict[str, Any]) -> None:
        task = await repo.get_task(str(row["task_id"]))
        state = TaskState(task["state"])
        now = self.now()
        if state in TASK_TERMINAL:
            await self._collect(repo, row, task)
            return
        if state == TaskState.WAITING:
            reason = task.get("waiting_reason")
            if reason == WaitingReason.RESOURCE:
                # 到了作者设的上限（第 8.4 节）：停下，作者可以加钱再来一轮
                await self._stop(repo, row, task, status="over_budget")
                return
            if reason == WaitingReason.ENVIRONMENT:
                await self._wait(repo, row, "computer_offline")
            else:
                await self._stop(
                    repo, row, task, status="failed", error=await self._why(repo, task)
                )
                return
        elif row.get("waiting_for"):
            async with repo.transaction():
                await repo.update_incubation(row["incubation_id"], waiting_for=None)
        if (now - row["started_at"]).total_seconds() > row["timeout_seconds"]:
            await self._stop(repo, row, task, status="failed", error="timed out")

    async def _why(self, repo: WorkbenchStore, task: dict[str, Any]) -> str:
        """委托停下来等人：孵化里没人答（要问作者的写成缺口），说清停在哪。"""
        interaction = (
            await repo.get_interaction(str(task["active_interaction_id"]))
            if task.get("active_interaction_id")
            else None
        )
        subject = ((interaction or {}).get("prompt") or {}).get("subject") or {}
        failures = subject.get("failures")
        if failures:
            return (
                "the handback did not pass its checks after one rework: "
                + "; ".join(str(f) for f in failures)
            )[:ERROR_CHARS]
        kind = (interaction or {}).get("kind") or task.get("waiting_reason")
        return f"stopped for a decision in the workbench ({kind})"

    async def _collect(
        self, repo: WorkbenchStore, row: dict[str, Any], task: dict[str, Any]
    ) -> None:
        """委托结束了：成了就取交回物照契约核，不成写明原因。"""
        state = TaskState(task["state"])
        spent = _used(task)
        if state == TaskState.CANCELLED:
            # 作者在自己的工作台里停的
            await self._finish(repo, row, status="cancelled", spent=spent)
            return
        if state != TaskState.SUCCEEDED:
            reason = task.get("reason") or {}
            why = next(
                (str(reason[k]) for k in ("error", "message", "code") if reason.get(k)),
                "no reason recorded",
            )
            await self._finish(
                repo,
                row,
                status="failed",
                error=f"the round ended as {state.lower()}: {why}"[:ERROR_CHARS],
                spent=spent,
            )
            return
        step = INCUBATE.workflow[0]
        found = await repo.get_artifact(
            task_id=str(task["id"]), name=step.output_artifact
        )
        try:
            handback = Handback.model_validate((found or {}).get("content"))
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            where = ".".join(str(p) for p in first.get("loc") or ())
            await self._finish(
                repo,
                row,
                status="failed",
                error=(
                    "the handback does not fit knowbot-incubation/v1: "
                    f"{where} {first.get('msg', '')}"
                )[:ERROR_CHARS],
                spent=spent,
            )
            return
        await self._finish(
            repo,
            row,
            status="succeeded",
            spent=spent,
            handback=handback.model_dump(mode="json"),
        )

    async def _stop(
        self,
        repo: WorkbenchStore,
        row: dict[str, Any],
        task: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        """停下在跑的委托并收尾（超预算、超时、停下等人）。"""
        await cancel_task(
            repo, str(task["id"]), owner_actor_id=str(task["owner_actor_id"])
        )
        await self._finish(repo, row, status=status, error=error, spent=_used(task))

    async def _wait(self, repo: WorkbenchStore, row: dict[str, Any], why: str) -> None:
        if row.get("waiting_for") == why:
            return
        log.info("incubation waiting id=%s for=%s", row["incubation_id"], why)
        async with repo.transaction():
            await repo.update_incubation(row["incubation_id"], waiting_for=why)
        row["waiting_for"] = why

    async def _finish(
        self,
        repo: WorkbenchStore,
        row: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
        spent: Decimal | None = None,
        handback: dict[str, Any] | None = None,
    ) -> None:
        changes: dict[str, Any] = {
            "status": status,
            "error": error,
            "finished_at": self.now(),
            "waiting_for": None,
            "handback": handback,
        }
        if spent is not None:
            changes["spent"] = spent
        async with repo.transaction():
            await repo.update_incubation(row["incubation_id"], **changes)
        log.info(
            "incubation finished id=%s status=%s error=%s",
            row["incubation_id"],
            status,
            error or "",
        )

    async def run_forever(self, stop: Any, interval_seconds: float) -> None:
        """runner 进程里的一条线：隔一会儿推一次在跑的孵化。"""
        import asyncio

        log.info("incubation conductor starting interval=%ss", interval_seconds)
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("incubation conductor iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
