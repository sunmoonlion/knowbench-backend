"""专家这一面给页面的读取：一个委托做到哪了、每一步交回了什么（investment-expert.md 第六节）。

只读。每次读取都重新核对归属（F-PROJ-08）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.catalog import pack_for_task
from app.domain.workbench.dossier_view import dossier_markdown, dossier_view
from app.domain.workbench.errors import NotFound
from app.domain.workbench.now_view import now_view
from app.domain.workbench.packs import ExpertPack
from app.domain.workbench.review_view import review_view
from app.domain.workbench.states import TASK_TERMINAL, TaskState
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
        pack = pack_for_task(task)
        attempts = await self.repo.list_attempts(task_id)
        return task, pack, attempts

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
        return steps_view(pack, task, attempts, verdicts, contents, items, only=only)

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
            types=("step/started", "thread/tokenUsage/updated"),
        )
        seen = None
        for event in events:
            if event["type"] == "step/started":
                # 找到这一步开始的地方；它之前的用量都不算
                seen = Decimal("0") if event.get("attempt_id") == str(active) else None
            elif seen is not None:
                seen = amount((event["payload"].get("cost") or {}).get("turn")) or seen
        return None if seen is None else str(seen)

    async def _now(
        self, task: dict[str, Any], steps: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """现在在干什么。看的是正在做的这一次开做之后的事件。"""
        task_id = str(task["id"])
        active = task.get("active_attempt_id")
        started = [
            e
            for e in await self.repo.list_task_events(task_id, types=("step/started",))
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
        return now_view(task, steps, events=events, since=since, held=held)

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
        return {
            "task": await self._sheet(task, pack, steps),
            "now": await self._now(task, steps),
            "position": None
            if task["state"] == TaskState.REJECTED
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
        latest = {
            row["name"]: row
            for row in await self.repo.list_artifacts_with_content(task_id)
        }
        missing = await self.repo.list_task_events(task_id, types=("data.missing",))
        return dossier_view(
            pack,
            await self._sheet(task, pack, steps),
            task,
            steps,
            None if task["state"] == TaskState.REJECTED else position(pack, task),
            {n: r for n, r in latest.items() if r["kind"] == "step"},
            latest.get("conclusion"),
            no_data=bool(missing),
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
            if pack is not None and int(task["current_step"]) < len(pack.workflow):
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
            pack = pack_for_task(task)
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
                "position": position(pack, task) if pack else None,
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
