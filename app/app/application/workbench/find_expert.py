"""AI 请专家（请专家方案设计第 4.9 节；专家孵化方案第十三节；decisions 10-29「AI 请专家」）。

- 「找专家」是一件内部小委托（方法包 FIND_EXPERT，同孵化一轮的做法）：在用户的项目里、用用户
  自己的模型和 key 跑一轮；模型用工具 `find_expert` 查 knowmart 目录（只有专家的公开信息），
  交回结果：summon（有合适的、不用另付钱的）/ candidates（合适的都要付钱，最多 3 位）/ none
  （都不合适，附推荐「直接答」或「孵化」与理由、挂需求的描述草稿）。工作台后端不调模型。
- 取结果时再按目录核一遍：交回的专家要真在这个用户的目录里、summon 的要真不用另付钱，再补上
  名称、价格这些给页面看。直接请由网页在用户在场时接着钉住、建委托（作者数据的授权本人签）。
- 「孵化一个专家」：经服务关系 `knowlab:incubation-request` 以用户的名义建一份「问题」申请；
  用户先让自己的模型直接答过的，那份回答附过去作参考（只算提议）。
- 挂需求：用户看过描述、点了「发布」，经 `knowmart:demand` 替他挂出；截止默认 30 天后。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.ledger import Ledger
from app.application.workbench.session_service import SessionService
from app.domain.workbench.errors import NotFound, WorkbenchError
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.packs import FIND_EXPERT
from app.domain.workbench.states import TASK_TERMINAL, TaskState

MAX_PICKS = 3
DEMAND_DAYS = 30
DEFAULT_BUDGET = Decimal("2")
QUESTION_CHARS = 2000
REFERENCE_CHARS = 20000


class FindExpertRefused(WorkbenchError):
    code = "find_expert_refused"
    http_status = 409


class ExpertDirectory(Protocol):
    def configured(self) -> bool: ...

    async def experts(self, owner_subject: str) -> list[dict[str, Any]]: ...


class DemandBoard(Protocol):
    async def publish(
        self, owner_subject: str, demand: dict[str, Any]
    ) -> dict[str, Any]: ...


class IncubationDesk(Protocol):
    async def submit(self, request: dict[str, Any]) -> dict[str, Any]: ...


# ---------- 模型交回的 ----------
class Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Pick(Strict):
    knowbot_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[1-9][0-9]{0,5}$")
    reason: str = Field(min_length=1, max_length=1000)


class Recommend(Strict):
    action: Literal["direct", "incubate"]
    reason: str = Field(min_length=1, max_length=1000)


class DemandDraft(Strict):
    title: str = Field(min_length=1, max_length=120)
    problem: str = Field(min_length=1, max_length=4000)


class FindResult(Strict):
    outcome: Literal["summon", "candidates", "none"]
    picks: tuple[Pick, ...] = Field(default=(), max_length=MAX_PICKS)
    recommend: Recommend | None = None
    demand: DemandDraft | None = None

    @model_validator(mode="after")
    def _fits_outcome(self) -> FindResult:
        if self.outcome == "summon" and len(self.picks) != 1:
            raise ValueError("summon picks exactly one expert")
        if self.outcome == "candidates" and not self.picks:
            raise ValueError("candidates needs at least one expert")
        if self.outcome == "none" and (self.picks or self.recommend is None):
            raise ValueError("none has no picks and carries a recommendation")
        return self


async def find_expert(
    repo: WorkbenchStore, directory: ExpertDirectory | None, working: dict[str, Any]
) -> dict[str, Any]:
    """工具 `find_expert`：这个用户请得动的专家的公开信息（不排序，按名称）。"""
    if directory is None or not directory.configured():
        return {"experts": [], "note": "the expert directory is not available now"}
    subject = await repo.subject_of_actor(str(working["owner_actor_id"]))
    if subject is None:
        return {"experts": [], "note": "the user is unknown to the directory"}
    return {"experts": [_public(e) for e in await directory.experts(subject)]}


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    scope = entry.get("scope")
    if not isinstance(scope, dict):
        scope = {}
    return {
        "knowbot_id": str(entry["knowbot_id"]),
        "name": str(entry.get("name") or ""),
        "tagline": str(entry.get("tagline") or ""),
        "version": str(entry.get("version") or ""),
        "scope": {
            k: str(scope.get(k) or "") for k in ("does", "does_not", "elsewhere")
        },
        "price": entry.get("price"),
        "free": bool(entry.get("free")),
    }


def task_key(project_id: str, question: str, nonce: str) -> str:
    raw = f"{project_id}\n{question}\n{nonce}".encode()
    return "find-expert-" + hashlib.sha256(raw).hexdigest()[:40]


class FindExpertService:
    def __init__(
        self,
        repo: WorkbenchStore,
        *,
        directory: ExpertDirectory | None = None,
        demands: DemandBoard | None = None,
        incubations: IncubationDesk | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.directory = directory
        self.demands = demands
        self.incubations = incubations
        self.now = now

    async def start(
        self,
        *,
        owner_actor_id: str,
        project_id: str,
        question: str,
        idempotency_key: str,
        budget_limit: Decimal = DEFAULT_BUDGET,
    ) -> dict[str, Any]:
        """开「找专家」那一轮：用户项目里一段专家对话、一件内部委托，排上去跑。"""
        text = question.strip()
        if not text or len(text) > QUESTION_CHARS:
            raise FindExpertRefused(f"the question is 1 to {QUESTION_CHARS} characters")
        if budget_limit <= 0:
            raise FindExpertRefused("the budget limit is more than zero")
        started = await SessionService(self.repo).start(
            owner_actor_id=owner_actor_id,
            kind="expert",
            project_id=project_id,
            title=f"找专家：{text}"[:200],
        )
        made = await Ledger(self.repo).handover(
            HandoverRequest(
                idempotency_key=task_key(project_id, text, idempotency_key),
                session_id=started["session_id"],
                profile_id=FIND_EXPERT.profile_id,
                original_input={"text": text},
                budget_limit=budget_limit,
                client_context={"entry": "find-expert"},
            ),
            owner_actor_id=owner_actor_id,
        )
        session = await self.repo.get_session(started["session_id"])
        async with self.repo.transaction():
            if not made.get("queued_behind"):
                await self.repo.enqueue_command(
                    session_id=started["session_id"],
                    sandbox_id=str(session["sandbox_id"]),
                    kind="task.drive",
                    payload={"task_id": made["task_id"]},
                )
        return {"task_id": made["task_id"], "session_id": started["session_id"]}

    async def _task(self, owner_actor_id: str, task_id: str) -> dict[str, Any]:
        task = await self.repo.get_task(task_id, owner_actor_id=owner_actor_id)
        if task.get("profile_id") != FIND_EXPERT.profile_id:
            raise NotFound("no such find-expert round")
        return task

    async def result(self, owner_actor_id: str, task_id: str) -> dict[str, Any]:
        """这一轮怎样了；成了的按目录核过、补上名称与价格。"""
        task = await self._task(owner_actor_id, task_id)
        state = TaskState(task["state"])
        question = _question(task)
        base = {
            "task_id": str(task["id"]),
            "project_id": str(task.get("project_id") or ""),
            "question": question,
            "state": state.value,
        }
        if state not in TASK_TERMINAL:
            return base
        if state != TaskState.SUCCEEDED:
            return base | {"error": "the round did not finish"}
        step = FIND_EXPERT.workflow[0]
        found = await self.repo.get_artifact(
            task_id=str(task["id"]), name=step.output_artifact
        )
        try:
            parsed = FindResult.model_validate((found or {}).get("content"))
        except ValidationError:
            return base | {"error": "the round handed back something unreadable"}
        listed = await self._listed(owner_actor_id)
        picks = []
        for pick in parsed.picks:
            entry = listed.get((pick.knowbot_id, pick.version))
            if entry is None:
                continue  # 目录里没有的不算（模型编的、刚下架的）
            picks.append(
                {
                    "knowbot_id": pick.knowbot_id,
                    "version": pick.version,
                    "name": entry["name"],
                    "tagline": entry["tagline"],
                    "price": entry["price"],
                    "free": entry["free"],
                    "reason": pick.reason,
                }
            )
        outcome = parsed.outcome
        if outcome == "summon" and not (picks and picks[0]["free"]):
            # 说是不用付钱的却核不上：退成候选，让用户自己点（花钱的由用户定）
            outcome = "candidates" if picks else "none"
        if outcome == "candidates" and not picks:
            outcome = "none"
        return base | {
            "outcome": outcome,
            "picks": picks if outcome != "none" else [],
            "recommend": None
            if parsed.recommend is None
            else parsed.recommend.model_dump(mode="json"),
            "demand": None
            if parsed.demand is None
            else parsed.demand.model_dump(mode="json"),
        }

    async def _listed(
        self, owner_actor_id: str
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if self.directory is None or not self.directory.configured():
            return {}
        subject = await self.repo.subject_of_actor(owner_actor_id)
        if subject is None:
            return {}
        return {
            (str(e["knowbot_id"]), str(e.get("version"))): _public(e)
            for e in await self.directory.experts(subject)
        }

    async def publish_demand(
        self,
        owner_actor_id: str,
        task_id: str,
        *,
        title: str,
        problem: str,
        deadline: date | None = None,
    ) -> dict[str, Any]:
        """用户看过描述、点了「发布」：替他在 knowmart 挂需求（要专家）。"""
        await self._task(owner_actor_id, task_id)
        if self.demands is None:
            raise FindExpertRefused("publishing demands is not configured")
        subject = await self.repo.subject_of_actor(owner_actor_id)
        if subject is None:
            raise NotFound("no such user")
        return await self.demands.publish(
            subject,
            {
                "kind": "expert",
                "title": title.strip()[:120],
                "problem": problem.strip()[:4000],
                "data_wanted": "",
                "deadline": (
                    deadline or (self.now().date() + timedelta(days=DEMAND_DAYS))
                ).isoformat(),
            },
        )

    async def ask_incubation(
        self,
        owner_actor_id: str,
        task_id: str,
        *,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """「孵化一个专家」：knowlab 以用户的名义建「问题」申请；先直接答过的那份回答附过去。"""
        task = await self._task(owner_actor_id, task_id)
        if self.incubations is None:
            raise FindExpertRefused("incubation requests are not configured")
        subject = await self.repo.subject_of_actor(owner_actor_id)
        if subject is None:
            raise NotFound("no such user")
        question = _question(task)
        reference = None
        if conversation_id is not None:
            reference = await self._last_answer(owner_actor_id, conversation_id)
        return await self.incubations.submit(
            {
                "owner_subject": subject,
                "question": question[:QUESTION_CHARS],
                "project_id": str(task.get("project_id") or ""),
                "task_id": str(task["id"]),
                "key": f"knowbench:find-expert:{task['id']}",
                **({"reference_answer": reference} if reference else {}),
            }
        )

    async def _last_answer(
        self, owner_actor_id: str, conversation_id: str
    ) -> str | None:
        """直接答那段对话里模型最后的回答（只取他自己的对话）。"""
        session = await self.repo.get_session(
            conversation_id, owner_actor_id=owner_actor_id
        )
        events = await self.repo.list_record_events(
            session_id=str(session["id"]), types=("item/completed",)
        )
        texts: Sequence[str] = [
            str(item.get("text") or "")
            for item in ((e.get("payload") or {}).get("item") for e in events)
            if isinstance(item, dict) and item.get("type") == "agentMessage"
        ]
        last = next((t for t in reversed(texts) if t.strip()), None)
        return None if last is None else last.strip()[:REFERENCE_CHARS]


def _question(task: dict[str, Any]) -> str:
    """委托里存的那句问题（交给专家时的原始输入 input.text）。"""
    given = (task.get("original_input") or {}).get("input") or {}
    return str(given.get("text") or "") if isinstance(given, dict) else ""
