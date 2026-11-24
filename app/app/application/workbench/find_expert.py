"""Match a user question against authorized, pinned formal expert declarations."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench import formal_expert_intake
from app.application.workbench.ledger import Ledger
from app.application.workbench.session_service import SessionService
from app.domain.workbench.errors import IdempotencyConflict, NotFound, WorkbenchError
from app.domain.workbench.expert_lookup import STOP_REASONS as LOOKUP_STOP_REASONS
from app.domain.workbench.expert_lookup import LegacyMatchRetired
from app.domain.workbench.formal_method_generated import MethodRef
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.packs import FIND_EXPERT
from app.domain.workbench.states import TASK_TERMINAL, TaskState

DEFAULT_BUDGET = Decimal("2")
QUESTION_CHARS = 2000


class FindExpertRefused(WorkbenchError):
    code = "find_expert_refused"
    http_status = 409


class ExpertDirectoryUnavailable(WorkbenchError):
    code = "expert_directory_unavailable"
    http_status = 503


class DemandBoard(Protocol):
    async def publish(
        self, owner_subject: str, demand: dict[str, Any]
    ) -> dict[str, Any]: ...


class IncubationDesk(Protocol):
    async def submit(self, request: dict[str, Any]) -> dict[str, Any]: ...


def task_key(project_id: str, question: str, nonce: str) -> str:
    raw = f"{project_id}\n{question}\n{nonce}".encode()
    return "find-expert-" + hashlib.sha256(raw).hexdigest()[:40]


class FindExpertService:
    def __init__(
        self,
        repo: WorkbenchStore,
        *,
        formal_registry=None,
        unified_entry_enabled: bool = False,
        planning_owners: frozenset[str] = frozenset(),
        material_read_enabled: bool = False,
        script_enabled: bool = False,
        instruction_service=None,
        demands: DemandBoard | None = None,
        incubations: IncubationDesk | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.unified_entry_enabled = unified_entry_enabled
        self.planning_owners = planning_owners
        self.formal_registry = formal_registry
        self.material_read_enabled = material_read_enabled
        self.script_enabled = script_enabled
        self.instruction_service = instruction_service
        self.demands = demands
        self.incubations = incubations
        self.now = now

    def planning_enabled(self, subject):
        return bool(
            subject and (self.unified_entry_enabled or subject in self.planning_owners)
        )

    async def start(
        self,
        *,
        owner_actor_id: str,
        project_id: str,
        question: str,
        idempotency_key: str,
        budget_limit: Decimal = DEFAULT_BUDGET,
        match_contract: str | None = None,
        reference: MethodRef | None = None,
        directory_offset: int = 0,
        retain_empty: bool = False,
        revises: str | None = None,
        source_session: str | None = None,
        followup: dict | None = None,
        checked_directory: dict | None = None,
    ) -> dict[str, Any]:
        """开「找专家」那一轮：用户项目里一段专家对话、一件内部委托，排上去跑。"""
        if match_contract != "knowbench/expert-match/v5":
            raise LegacyMatchRetired()
        if not self.planning_enabled(await self.repo.subject_of_actor(owner_actor_id)):
            raise FindExpertRefused("此账号尚未开放正式专家匹配。")
        text = question.strip()
        if not text or len(text) > QUESTION_CHARS:
            raise FindExpertRefused(f"the question is 1 to {QUESTION_CHARS} characters")
        if budget_limit <= 0:
            raise FindExpertRefused("the budget limit is more than zero")
        if followup is not None or checked_directory is not None:
            if (
                not reference
                or not followup
                or not checked_directory
                or [e["reference"] for e in checked_directory["items"]]
                != [reference.model_dump(mode="json")]
            ):
                raise FindExpertRefused(
                    "A linked request requires one checked expert version"
                )
        if directory_offset and reference is not None:
            raise FindExpertRefused(
                "Pagination requires an unnamed formal expert search"
            )
        key = task_key(project_id, text, idempotency_key)

        def request(session_id: str) -> HandoverRequest:
            return HandoverRequest(
                idempotency_key=key,
                session_id=session_id,
                profile_id=FIND_EXPERT.profile_id,
                profile_version="5",
                original_input={
                    "text": text,
                    **({"followup": followup} if followup else {}),
                    **({"retain_empty": True} if retain_empty else {}),
                    **(
                        {"directory_offset": directory_offset}
                        if directory_offset
                        else {}
                    ),
                    **({"source_session": source_session} if source_session else {}),
                    **({"revises": revises} if revises else {}),
                    **(
                        {"reference": reference.model_dump(mode="json")}
                        if reference
                        else {}
                    ),
                },
                budget_limit=budget_limit,
                client_context={"entry": "find-expert"},
            )

        directory = None
        async with self.repo.transaction():
            await self.repo.get_project(project_id, owner_actor_id=owner_actor_id)
            if source_session:
                session = await self.repo.get_session(
                    source_session, owner_actor_id=owner_actor_id
                )
                if (
                    session.get("project_id") is not None
                    and str(session["project_id"]) != project_id
                ):
                    raise NotFound("No such source session in this project")
            existing = await self.repo.find_task_by_idempotency(
                tenant="default",
                owner_actor_id=owner_actor_id,
                profile_id=FIND_EXPERT.profile_id,
                idempotency_key=key,
            )
        if existing is None:
            if self.formal_registry is None:
                raise ExpertDirectoryUnavailable("Formal expert directory unavailable")
            directory = checked_directory or await formal_expert_intake.directory_for(
                self.formal_registry,
                owner_actor_id,
                text,
                reference,
                offset=directory_offset,
            )
            if not directory["items"] and (
                not retain_empty or directory["next"] is not None
            ):
                if revises:
                    async with self.repo.transaction():
                        await formal_expert_intake.previous_round(
                            self.repo, owner_actor_id, project_id, revises
                        )
                return {"empty": True, "next": directory["next"]}
        async with self.repo.transaction():
            # 同项目的重复请求串行核幂等，再建新会话；会话、委托与派发命令同提交。
            # 不能先建会话：session_id 参与摘要，重发会冲突并遗留空会话。
            await self.repo.get_project(
                project_id, owner_actor_id=owner_actor_id, for_update=True
            )
            prior = await self.repo.find_task_by_idempotency(
                tenant="default",
                owner_actor_id=owner_actor_id,
                profile_id=FIND_EXPERT.profile_id,
                idempotency_key=key,
            )
            if prior is not None:
                if (
                    prior["request_digest"]
                    != request(str(prior["session_id"])).digest()
                ):
                    raise IdempotencyConflict(
                        "same idempotency key with a different request",
                        task_id=str(prior["id"]),
                    )
                return {
                    "task_id": str(prior["id"]),
                    "session_id": str(prior["session_id"]),
                }
            previous = None
            if revises:
                previous = await formal_expert_intake.previous_round(
                    self.repo, owner_actor_id, project_id, revises
                )
            if directory is None:
                raise FindExpertRefused("Matching request changed, retry")
            started = await SessionService(self.repo).start(
                owner_actor_id=owner_actor_id,
                kind="expert",
                project_id=project_id,
                title=f"找专家：{text}"[:200],
            )
            session = await self.repo.get_session(started["session_id"])
            host_result = directory is not None and not directory["items"]
            if host_result:
                made = await formal_expert_intake.record_empty(
                    self.repo, owner_actor_id, session, request(started["session_id"])
                )
            else:
                made = await Ledger(self.repo).handover(
                    request(started["session_id"]), owner_actor_id=owner_actor_id
                )
            if previous is not None:
                await formal_expert_intake.cancel_planner(
                    self.repo, owner_actor_id, str(previous["id"])
                )
                await self.repo.append_event(
                    session_id=str(previous["session_id"]),
                    kind="task",
                    task_id=str(previous["id"]),
                    event_type=formal_expert_intake.REVISION,
                    payload={"task_id": made["task_id"]},
                )
            if directory is not None:
                await self.repo.append_event(
                    session_id=started["session_id"],
                    kind="task",
                    task_id=made["task_id"],
                    event_type=formal_expert_intake.DIRECTORY,
                    payload=directory,
                )
            if not host_result and not made.get("queued_behind"):
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
        if str(task.get("profile_version")) != "5":
            raise LegacyMatchRetired()
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
        from app.domain.workbench.ordinary_expert import CONFIRMED

        base["session_id"] = str(task["session_id"])
        if followup := task["original_input"]["input"].get("followup"):
            base["followup"] = followup
        execution = await self.repo.latest_task_payload(task_id, CONFIRMED)
        if execution:
            delegated = await self.repo.get_task(
                execution["task_id"], owner_actor_id=owner_actor_id
            )
            delivery = None
            if delegated["state"] == TaskState.SUCCEEDED:
                output = await self.repo.get_artifact(
                    task_id=execution["task_id"], name="expert_delivery", version=1
                )
                if output is None or str(output["id"]) != str(
                    delegated["terminal_result_ref"]
                ):
                    raise FindExpertRefused(
                        "The confirmed expert delivery could not be verified"
                    )
                delivery = output["content"]
            from app.application.workbench.formal_handoff import context

            evidence = await self.repo.get_artifact(task_id=task_id, name="find_expert")
            review = await formal_expert_intake.read_verified(self.repo, task, evidence)
            from app.application.workbench.formal_expert_preparation import (
                PREPARATION,
            )

            preparation = await self.repo.latest_task_payload(task_id, PREPARATION)
            return base | {
                "duty_material_slots": sorted(preparation["bindings"]["materials"])
                if preparation
                else [],
                "temporary_delivery": bool(
                    await self.repo.latest_task_payload(
                        task_id, "task/temporary_plan_adopted"
                    )
                ),
                "feedback": await context(self.repo, task, review["review_digest"]),
                "execution": execution,
                "execution_state": delegated["state"],
                "delivery": delivery,
            }
        revised = await self.repo.list_task_events(
            task_id, types=(formal_expert_intake.REVISION,)
        )
        if revised:
            return base | {"superseded_by": revised[0]["payload"]["task_id"]}
        if state not in TASK_TERMINAL:
            return base
        if state != TaskState.SUCCEEDED:
            events = await self.repo.list_task_events(task_id, types=("task/state",))
            reason = events[-1]["payload"].get("reason", {}) if events else {}
            message = (
                LOOKUP_STOP_REASONS.get(str(reason.get("error") or ""))
                if reason.get("code") == "expert_lookup_failed"
                else None
            )
            if reason.get("code") == "expert_lookup_stop_unconfirmed":
                message = "查找中断尚未确认，平台正在核对；请勿重复提交"
            return base | {"error": message or "the round did not finish"}
        step = FIND_EXPERT.workflow[0]
        found = await self.repo.get_artifact(
            task_id=str(task["id"]), name=step.output_artifact
        )
        reviewed = await formal_expert_intake.review(
            self.repo, self.formal_registry, owner_actor_id, task, found
        )
        reviewed["planning_available"] = self.planning_enabled(
            await self.repo.subject_of_actor(owner_actor_id)
        )
        return base | {"formal": reviewed}


def _question(task: dict[str, Any]) -> str:
    """委托里存的那句问题（交给专家时的原始输入 input.text）。"""
    given = (task.get("original_input") or {}).get("input") or {}
    return str(given.get("text") or "") if isinstance(given, dict) else ""
