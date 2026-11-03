"""专家这一面给页面的读取：一个委托做到哪了、每一步交回了什么（investment-expert.md 第六节）。

只读。每次读取都重新核对归属（F-PROJ-08）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.execution_plan import (
    current_artifacts,
    display_for_task,
    execution_for_task,
    is_dynamic,
)
from app.application.workbench.expert_data import pending_requests
from app.application.workbench.plan_receipts import read_plan_version
from app.domain.workbench.data_planning import DATA_STEP_ID
from app.domain.workbench.dossier_view import dossier_markdown, dossier_view
from app.domain.workbench.errors import NotFound
from app.domain.workbench.method_plan import PLAN_STEP
from app.domain.workbench.now_view import now_view
from app.domain.workbench.packs import ExpertPack
from app.domain.workbench.review_view import review_view
from app.domain.workbench.states import TASK_TERMINAL, TaskState, WaitingReason
from app.domain.workbench.step_view import (
    TERMINAL_WORDS,
    amount,
    budget_view,
    position,
    reason_text,
    steps_view,
)

VERDICTS = ("step/accepted", "step/rejected")


def question_of(task: dict[str, Any]) -> str:
    """用户交出去的原话。"""
    given = (task.get("original_input") or {}).get("input") or {}
    if isinstance(given, str):
        return given
    for key in ("text", "question", "q"):
        if isinstance(given.get(key), str) and given[key].strip():
            return given[key].strip()
    return ""


def data_seen(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """这个委托用到的数据：各步交回物里写明的合在一起。

    各步写的数据版本本该相同。不同就是做的中途数据换了版本：如实列出来，由页面提醒。
    """
    merged: dict[str, Any] = {}
    versions: list[str] = []
    for step in steps:
        found = step.get("data") or {}
        merged.update(found)
        version = found.get("data_version")
        if version and version not in versions:
            versions.append(str(version))
    if not merged:
        return None
    return {**merged, "versions_seen": versions}


class ExpertDesk:
    def __init__(self, repo: WorkbenchStore):
        self.repo = repo

    async def _load(
        self, task_id: str, owner_actor_id: str
    ) -> tuple[dict[str, Any], ExpertPack | None, list[dict[str, Any]]]:
        task = await self.repo.get_task(task_id, owner_actor_id=owner_actor_id)
        pack = await display_for_task(self.repo, task)
        attempts = await self.repo.list_attempts(task_id)
        return task, pack, attempts

    async def _preparing_data(
        self,
        task: dict[str, Any],
        attempts: list[dict[str, Any]],
    ) -> bool:
        # Once analysis starts, supplemental data waits belong to that real step.
        if task["current_step"] != 0 or any(
            a["step_id"] not in (DATA_STEP_ID, PLAN_STEP) for a in attempts
        ):
            return False
        if any(a["step_id"] == DATA_STEP_ID for a in attempts):
            return True
        row = await self.repo.get_task_resolution(str(task["id"]))
        return bool(row and row["status"] == "planning")

    async def _steps(
        self,
        task: dict[str, Any],
        pack: ExpertPack,
        attempts: list[dict[str, Any]],
        *,
        only: int | None,
    ) -> list[dict[str, Any]]:
        task_id = str(task["id"])
        verdicts = {
            str(e["attempt_id"]): e["payload"]
            for e in await self.repo.list_task_events(task_id, types=VERDICTS)
            if e.get("attempt_id")
        }
        wanted = attempts
        if only is not None:
            step_id = pack.workflow[only - 1].step_id
            wanted = [a for a in attempts if a.get("step_id") == step_id]
        turns = [str(t) for a in wanted for t in (a.get("turn_ids") or [])]
        items = await self.repo.list_turn_items(
            session_id=str(task["session_id"]), turn_ids=turns
        )
        contents = await self.repo.artifact_contents(task_id)
        shown = steps_view(pack, task, attempts, verdicts, contents, items, only=only)
        if is_dynamic(task) and task.get("accepted_plan_version"):
            view = await execution_for_task(self.repo, task)
            assert view and view.receipt
            current = await current_artifacts(self.repo, task)
            active = next(
                (
                    a
                    for a in attempts
                    if str(a["id"]) == str(task.get("active_attempt_id"))
                ),
                None,
            )
            planning = active and active["step_id"] == PLAN_STEP
            for row in shown:
                index = row["index"] - 1
                instance = view.receipt.proposal.steps[index]
                art = current.get(pack.workflow[index].output_artifact)
                row["selection_reason"] = instance.reason
                row["method_instance"] = instance.instance_id
                row["current_result"] = (
                    {k: art[k] for k in ("id", "version", "digest")} if art else None
                )
                if art is None:
                    row["data"] = None  # Old accepted history is not current evidence.
                if planning and index == int(task["current_step"]):
                    row["status"] = "pending"
        if await self._preparing_data(task, attempts):
            for row in shown:
                row["status"] = (
                    "not_reached" if task["state"] in TASK_TERMINAL else "pending"
                )
        return shown

    async def _plan(self, task: dict[str, Any]) -> dict[str, Any] | None:
        if not is_dynamic(task):
            return None
        history = []
        for version in range(1, int(task.get("accepted_plan_version") or 0) + 1):
            receipt = await read_plan_version(self.repo, task, version)
            history.append({"version": version, "reason": receipt.proposal.reason})
        active = task.get("active_attempt_id")
        attempt = await self.repo.get_attempt(str(active)) if active else None
        return {
            "version": task.get("accepted_plan_version"),
            "planning": bool(attempt and attempt["step_id"] == PLAN_STEP),
            "reason": history[-1]["reason"] if history else None,
            "history": history,
        }

    async def _reason(self, task: dict[str, Any]) -> dict[str, Any] | None:
        """为什么停在现在这个状态。打回的原因在委托上；其余的在最后一次状态变化的事件里。"""
        if task["state"] == TaskState.REJECTED and task.get("rejection"):
            return dict(task["rejection"])
        changes = await self.repo.list_task_events(
            str(task["id"]), types=("task/state",)
        )
        for event in reversed(changes):
            if event["payload"].get("to") == task["state"]:
                return dict(event["payload"].get("reason") or {}) or None
        return None

    async def _running_cost(self, task: dict[str, Any]) -> str | None:
        """正在做的这一步到现在花了多少。做完才记进 `used`，在那之前从用量事件里看。"""
        active = task.get("active_attempt_id")
        if task["state"] != TaskState.RUNNING or not active:
            return None
        events = await self.repo.list_record_events(
            session_id=str(task["session_id"]),
            types=("attempt/opened", "step/started", "thread/tokenUsage/updated"),
        )
        seen = None
        for event in events:
            if event["type"] in ("attempt/opened", "step/started"):
                # 找到这一步开始的地方；它之前的用量都不算
                seen = Decimal("0") if event.get("attempt_id") == str(active) else None
            elif seen is not None:
                seen = amount((event["payload"].get("cost") or {}).get("turn")) or seen
        return None if seen is None else str(seen)

    async def _now(
        self,
        task: dict[str, Any],
        steps: list[dict[str, Any]],
        *,
        preparing: bool = False,
    ) -> dict[str, Any] | None:
        """现在在干什么。看的是正在做的这一次开做之后的事件。"""
        task_id = str(task["id"])
        active = task.get("active_attempt_id")
        started = [
            e
            for e in await self.repo.list_task_events(
                task_id, types=("attempt/opened", "step/started")
            )
            if active and e.get("attempt_id") == str(active)
        ]
        events: list[dict[str, Any]] = []
        since = task.get("updated_at")
        if started:
            since = started[-1]["created_at"]
            events = await self.repo.list_events(
                session_id=str(task["session_id"]),
                after_cursor=int(started[-1]["cursor"]),
                limit=1000,
            )
        session = await self.repo.get_session(str(task["session_id"]))
        held = await self.repo.sandbox_held(str(session["sandbox_id"]))
        row = (
            await self.repo.get_task_resolution(task_id)
            if task["state"] == TaskState.WAITING
            and task.get("waiting_reason") == WaitingReason.DATA
            else None
        )
        _, waiting = board_place(task, row)
        attempt = await self.repo.get_attempt(str(active)) if active else None
        if (
            attempt
            and attempt["step_id"] == PLAN_STEP
            and task["state"] not in TASK_TERMINAL
        ):
            waiting = task["state"] == TaskState.WAITING
            return {
                "step": {
                    "index": 0,
                    "of": len(steps),
                    "title": "规划任务",
                    "summary": "选择已确定的方法",
                    "why": "依据这次问题选择专家已有的方法，并确定执行顺序",
                },
                "doing": {
                    "code": "waiting" if waiting else "planning",
                    "text": "规划已暂停，等待恢复"
                    if waiting
                    else "正在为这次问题安排方法与顺序",
                },
                "redo": None,
                "since": since,
                "last_event_at": events[-1]["created_at"] if events else since,
                "held": held,
            }
        view = now_view(
            task,
            steps,
            events=events,
            since=since,
            held=held,
            waiting_for_data_service=bool(
                row and waiting and waiting["who"] == "data_service"
            ),
        )

        if view and preparing:
            view["step"].update(
                index=0,
                title="定数据",
                summary="先提出本次问题需要的数据",
                why="先列出数据需求供你确认，获准的数据准备好后才开始正式分析",
            )
            if task["state"] == TaskState.RUNNING:
                view["doing"] = {
                    "code": "planning",
                    "text": "正在整理数据需求，正式分析尚未开始",
                }
        return view

    async def _sheet(
        self,
        task: dict[str, Any],
        pack: ExpertPack | None,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """委托单：问题、预算、数据、时间。"""
        state = task["state"]
        ended = task.get("updated_at") if state in TASK_TERMINAL else None
        started = task.get("created_at")
        until = ended or datetime.now(UTC)
        data = data_seen(steps)
        return {
            "task_id": str(task["id"]),
            "session_id": str(task["session_id"]),
            "project_id": str(task["project_id"]) if task.get("project_id") else None,
            "expert": {
                "id": task["profile_id"],
                "name": (pack.name or pack.title) if pack else "",
                "version": task.get("expert_pack_version") or task["profile_version"],
            },
            "question": question_of(task),
            "state": state,
            "state_word": TERMINAL_WORDS.get(state),
            "waiting_reason": task.get("waiting_reason"),
            "cancel_requested": task.get("cancel_requested_at") is not None,
            "reason": (reason := await self._reason(task)),
            "reason_text": reason_text(state, reason) if state in TASK_TERMINAL else "",
            "budget": budget_view(
                task["budget"], running=await self._running_cost(task)
            ),
            "data": data,  # 第 2 步做完之前是 None：还不知道
            # 请专家时选的、钉进这件委托的数据（SDD 0042）：哪一样、名字、从哪来
            "chosen_data": [
                {"need": d["slot"], "name": d["name"], "source": d["source"]}
                for d in await self.repo.list_task_data(str(task["id"]))
            ],
            "started_at": started,
            "ended_at": ended,
            "seconds": int((until - started).total_seconds()) if started else None,
            "active_interaction_id": str(task["active_interaction_id"])
            if task.get("active_interaction_id")
            else None,
            **({"plan": await self._plan(task)} if is_dynamic(task) else {}),
        }

    async def steps(self, task_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        task, pack, attempts = await self._load(task_id, owner_actor_id)
        if pack is None:  # 受理时就打回了：没有步骤可言
            return {
                "task": await self._sheet(task, None, []),
                "now": None,
                "position": None,
                "steps": [],
            }
        steps = await self._steps(task, pack, attempts, only=None)
        preparing = await self._preparing_data(task, attempts)
        return {
            "task": await self._sheet(task, pack, steps),
            "now": await self._now(task, steps, preparing=preparing),
            "position": None
            if preparing
            or task["state"] in (TaskState.RECEIVED, TaskState.REJECTED)
            or not pack.workflow
            else position(pack, task),
            "steps": steps,
        }

    async def step(
        self, task_id: str, number: int, *, owner_actor_id: str
    ) -> dict[str, Any]:
        task, pack, attempts = await self._load(task_id, owner_actor_id)
        if pack is None or not 1 <= number <= len(pack.workflow):
            raise NotFound("no such step", task_id=task_id, step=number)
        (step,) = await self._steps(task, pack, attempts, only=number)
        return {"task_id": str(task["id"]), "step": step}

    # ---------------- 底稿 ----------------
    async def dossier(self, task_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        """底稿：进行中也能看，只有做完的部分。"""
        task, pack, attempts = await self._load(task_id, owner_actor_id)
        if pack is None:
            sheet = await self._sheet(task, None, [])
            return {
                "task": sheet,
                "head": {"kind": "refused", "text": "专家没有接"},
                "sections": [],
                "how": [],
                "conclusion": {
                    "text": "",
                    "kind": "user_draft",
                    "saved_at": None,
                    "version": None,
                },
            }
        steps = await self._steps(task, pack, attempts, only=None)
        current = await current_artifacts(self.repo, task)
        conclusion = await self.repo.get_artifact(task_id=task_id, name="conclusion")
        finals = None
        if is_dynamic(task):
            finals = ()
            if task.get("accepted_plan_version"):
                view = await execution_for_task(self.repo, task)
                assert view is not None
                finals = view.final_artifacts
        return dossier_view(
            pack,
            await self._sheet(task, pack, steps),
            task,
            steps,
            None if task["state"] == TaskState.REJECTED else position(pack, task),
            current,
            conclusion,
            final_artifacts=finals,
        )

    async def dossier_export(self, task_id: str, *, owner_actor_id: str) -> str:
        return dossier_markdown(
            await self.dossier(task_id, owner_actor_id=owner_actor_id)
        )

    # ---------------- 等我决定的事 ----------------
    async def _review(
        self, interaction: dict[str, Any], *, owner_actor_id: str, summary: bool
    ) -> dict[str, Any]:
        conversation = await self.repo.get_session(
            str(interaction["session_id"]), owner_actor_id=owner_actor_id
        )
        project = None
        if conversation.get("project_id"):
            project = await self.repo.get_project(str(conversation["project_id"]))
        task = pack = step = None
        question = ""
        if interaction.get("task_id"):
            task, pack, attempts = await self._load(
                str(interaction["task_id"]), owner_actor_id
            )
            question = question_of(task)
            active = next(
                (
                    a
                    for a in attempts
                    if str(a["id"]) == str(task.get("active_attempt_id"))
                ),
                None,
            )
            method_phase = not is_dynamic(task) or bool(
                active and (active.get("refs") or {}).get("kind") == "method_step"
            )
            if (
                method_phase
                and not await self._preparing_data(task, attempts)
                and pack is not None
                and int(task["current_step"]) < len(pack.workflow)
            ):
                (step,) = await self._steps(
                    task, pack, attempts, only=int(task["current_step"]) + 1
                )
        return review_view(
            interaction,
            now=datetime.now(UTC),
            project=project,
            conversation=conversation,
            task=task,
            pack=pack,
            step=step,
            question=question,
            opened_cursor=None
            if summary
            else await self.repo.interaction_opened_cursor(
                session_id=str(interaction["session_id"]),
                interaction_id=str(interaction["id"]),
            ),
            summary=summary,
        )

    async def waiting_for_me(
        self, *, owner_actor_id: str, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        """跨对话的待办：摘要形态（在哪、为什么、哪几条没过、什么时候过期）。"""
        rows = await self.repo.list_owner_interactions(
            owner_actor_id=owner_actor_id, status=status
        )
        return [
            await self._review(row, owner_actor_id=owner_actor_id, summary=True)
            for row in rows
        ]

    async def review(
        self, interaction_id: str, *, owner_actor_id: str
    ) -> dict[str, Any]:
        interaction = await self.repo.get_interaction(interaction_id)
        # 归属按对话认；不是自己的，和不存在一样答
        try:
            return await self._review(
                interaction, owner_actor_id=owner_actor_id, summary=False
            )
        except NotFound:
            raise NotFound(
                "interaction not found", interaction_id=interaction_id
            ) from None

    # ---------------- 专家首页 ----------------
    async def overview(
        self, *, owner_actor_id: str, recent: int = 10
    ) -> dict[str, Any]:
        """专家首页的三张清单：等我决定、进行中、最近交回的。"""
        waiting = [
            w
            for w in await self.waiting_for_me(owner_actor_id=owner_actor_id)
            if w["where"]["task"] is not None
        ]
        asked = {w["where"]["task"]["id"] for w in waiting}
        running: list[dict[str, Any]] = []
        returned: list[dict[str, Any]] = []
        for task in await self.repo.list_tasks(owner_actor_id=owner_actor_id):
            pack = await display_for_task(self.repo, task)
            ended = task["state"] in TASK_TERMINAL
            if ended and len(returned) >= recent:
                continue
            line = {
                "task_id": str(task["id"]),
                "session_id": str(task["session_id"]),
                "project_id": str(task["project_id"])
                if task.get("project_id")
                else None,
                "question": question_of(task),
                "expert": (pack.name or pack.title) if pack else "",
                "state": task["state"],
                "state_word": TERMINAL_WORDS.get(task["state"]),
                "waiting_reason": task.get("waiting_reason"),
                # 排在同项目前一位专家后面、还没开始的（0017 专家团：一次一位）
                "queued_behind": (
                    (task.get("original_input") or {}).get("client_context") or {}
                ).get("queued_behind"),
                "position": position(pack, task) if pack and pack.workflow else None,
                "budget": budget_view(task["budget"]),
                "started_at": task.get("created_at"),
                "ended_at": task.get("updated_at") if ended else None,
            }
            if ended:
                line["reason"] = await self._reason(task)
                line["reason_text"] = reason_text(task["state"], line["reason"])
                returned.append(line)
            elif str(task["id"]) not in asked:
                # 等我决定的已经列在上面了，这里不再列一遍。
                # 等机器回来的没有待办，留在这里，页面标「暂停」
                running.append(line)
        return {"waiting": waiting, "running": running, "returned": returned}

    # ---------------- 项目的看板 ----------------
    async def board(self, project_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        """项目的看板（请专家方案设计第 4.8 节）：这个项目的委托按列排，卡片写清等谁、
        等什么。别人的项目和没有的一样答。"""
        await self.repo.get_project(project_id, owner_actor_id=owner_actor_id)
        columns: dict[str, list[dict[str, Any]]] = {c: [] for c in BOARD_COLUMNS}
        for task in await self.repo.list_tasks(
            owner_actor_id=owner_actor_id, project_id=project_id
        ):
            pack = await display_for_task(self.repo, task)
            row = (
                await self.repo.get_task_resolution(str(task["id"]))
                if task["state"] == TaskState.WAITING
                else None
            )
            column, waiting = board_place(task, row)
            ended = task["state"] in TASK_TERMINAL
            columns[column].append(
                {
                    "task_id": str(task["id"]),
                    "session_id": str(task["session_id"]),
                    "question": question_of(task),
                    "expert": (pack.name or pack.title) if pack else "",
                    "state": task["state"],
                    "state_word": TERMINAL_WORDS.get(task["state"]),
                    "waiting": waiting,
                    "position": position(pack, task)
                    if pack and pack.workflow
                    else None,
                    "budget": budget_view(task["budget"]),
                    "started_at": task.get("created_at"),
                    "ended_at": task.get("updated_at") if ended else None,
                }
            )
        return {
            "project_id": project_id,
            "columns": [{"key": c, "cards": columns[c]} for c in BOARD_COLUMNS],
        }


# 看板的列：排队、在跑、等你、等别人、完成、没成（失败、打回、取消）
BOARD_COLUMNS = ("queued", "running", "waiting_you", "waiting_others", "done", "ended")


def board_place(
    task: dict[str, Any], row: dict[str, Any] | None
) -> tuple[str, dict[str, str] | None]:
    """一件委托在看板的哪一列、等谁（you 用户、data_service 数据服务）、等什么。"""
    state = task["state"]
    if state == TaskState.SUCCEEDED:
        return "done", None
    if state in TASK_TERMINAL:
        return "ended", None
    if state == TaskState.RUNNING:
        return "running", None
    if state != TaskState.WAITING:
        return "queued", None
    reason = task.get("waiting_reason")
    if reason == WaitingReason.DATA:
        statuses = {str(r.get("status")) for r in pending_requests(row)}
        if "pending" in statuses:
            return "waiting_you", {"who": "you", "what": "confirm_data"}
        if "missing" in statuses:
            return "waiting_you", {"who": "you", "what": "decide_missing"}
        return "waiting_others", {"who": "data_service", "what": "fetch_data"}
    what_by_reason: dict[str | None, str] = {
        WaitingReason.INPUT: "answer",
        WaitingReason.APPROVAL: "approve",
        WaitingReason.RESOURCE: "budget",
        WaitingReason.ENVIRONMENT: "machine",
    }
    return "waiting_you", {"who": "you", "what": what_by_reason.get(reason, "answer")}
