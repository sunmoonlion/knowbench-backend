"""账房：Task/Attempt 唯一的状态转换入口、预算账、Interaction 的签发与原子消费。

每个方法都在一个事务里把「状态 + 事件 + 关联账」一起提交（I4、I12、F-LEDGER-01）。
调用方（网页接口、编排、扫描器）不得绕开它直接改表。
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.catalog import PinnedPack, pack_for_task, pinned_pack
from app.domain.workbench.errors import (
    BudgetExhausted,
    EnvironmentOffline,
    IdempotencyConflict,
    InteractionRejected,
    ProjectRequired,
    StaleStateVersion,
    WheelHeldByOther,
)
from app.domain.workbench.knowbots import is_knowbot
from app.domain.workbench.models import Budget, HandoverRequest, InteractionPrompt
from app.domain.workbench.packs import current_pack_version
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    PLATFORM_FAILURE,
    TASK_TERMINAL,
    AttemptRole,
    AttemptState,
    TaskState,
    WaitingReason,
    Wheel,
    validate_attempt_transition,
    validate_task_transition,
    wheel_after_task_state,
)
from app.domain.workbench.step_view import reason_text
from app.domain.workbench.tokens import token_hash

INTERACTION_TTL = timedelta(hours=72)


def _budget(row: dict[str, Any]) -> Budget:
    b = row["budget"]
    return Budget(
        currency=b.get("currency", "CNY"),
        limit=None if b.get("limit") is None else Decimal(str(b["limit"])),
        reserved=Decimal(str(b.get("reserved", "0"))),
        used=Decimal(str(b.get("used", "0"))),
    )


def writes_files(task: dict[str, Any]) -> bool:
    """这件委托的专家会不会往项目里写成稿文件（0015 §3.3）。认不出专家包时按会写算，照旧指向底稿。"""
    pack = pack_for_task(task)
    return pack is None or bool(pack.deliverable_files())


class Ledger:
    def __init__(self, repo: WorkbenchStore):
        self.repo = repo

    # ---------------- 建单 = 交出方向盘 ----------------
    async def handover(
        self,
        req: HandoverRequest,
        *,
        owner_actor_id: str,
        tenant: str = "default",
        profile_version: str | None = None,
        expert_pack_version: str | None = None,
        snapshot: PinnedPack | None = None,
    ) -> dict[str, Any]:
        """F-INTAKE-01/02、F-ADMIT-01、F-WHEEL-01：幂等建单、首事件、wheel=advisor，同一提交。

        snapshot：调用方已经核好的专家包快照（跑分身测试时是草稿，SDD 0027 §3.4）。
        """
        digest = req.digest()
        async with self.repo.transaction():
            prior = await self.repo.find_task_by_idempotency(
                tenant=tenant,
                owner_actor_id=owner_actor_id,
                profile_id=req.profile_id,
                idempotency_key=req.idempotency_key,
            )
            if prior is not None:
                if prior["request_digest"] != digest:
                    raise IdempotencyConflict(
                        "same idempotency key with a different request",
                        task_id=str(prior["id"]),
                    )
                return {
                    "task_id": str(prior["id"]),
                    "state": prior["state"],
                    "created": False,
                }
            session = await self.repo.get_session(
                req.session_id, owner_actor_id=owner_actor_id, for_update=True
            )
            if session["wheel"] != Wheel.user or session["active_task_id"] is not None:
                raise WheelHeldByOther(
                    "the advisor already holds the wheel of this session",
                    session_id=req.session_id,
                )
            if session["project_id"] is None:
                raise ProjectRequired(
                    "put this conversation into a project before asking the expert",
                    session_id=req.session_id,
                )
            environment = await self.repo.get_environment(
                str(session["environment_id"])
            )
            if environment.get("status") != "online":
                raise EnvironmentOffline(
                    "the machine of this project is offline; the expert works on it",
                    environment_id=str(session["environment_id"]),
                )
            # 先锁住项目：同一个项目同一时刻只跑一位专家，后到的排队（0017，所有者 2026-10-11）
            await self.repo.get_project(str(session["project_id"]), for_update=True)
            busy = await self.repo.active_task_of_project(str(session["project_id"]))
            queued_behind = str(busy["id"]) if busy is not None else None
            # 分身：按这个人钉住的那一版建委托，专家包快照与清单摘要写进委托（SDD 0027 P4）。
            # 别人的、没钉过的分身在这里就是「没有这位专家」
            pinned = snapshot or (
                await pinned_pack(
                    self.repo, owner_actor_id=owner_actor_id, profile_id=req.profile_id
                )
                if is_knowbot(req.profile_id)
                else None
            )
            task_id = str(uuid.uuid4())
            budget = Budget(currency=req.budget_currency, limit=req.budget_limit)
            client_context = dict(req.client_context)
            if queued_behind:
                client_context["queued_behind"] = queued_behind
            await self.repo.insert_task(
                {
                    "id": task_id,
                    "session_id": req.session_id,
                    "owner_actor_id": owner_actor_id,
                    "tenant": tenant,
                    "idempotency_key": req.idempotency_key,
                    "request_digest": digest,
                    "profile_id": req.profile_id,
                    "profile_version": pinned.pack.version
                    if pinned
                    else (
                        req.profile_version
                        or profile_version
                        or current_pack_version(req.profile_id)
                    ),
                    **(pinned.task_columns() if pinned else {}),
                    "expert_pack_version": expert_pack_version,
                    "original_input": {
                        "input": req.original_input,
                        "attachments": req.attachments,
                        "client_context": client_context,
                    },
                    "thread_id": session["thread_id"],
                    "environment_id": str(session["environment_id"]),
                    "project_root": session["project_root"],
                    "project_id": str(session["project_id"]),
                    "state": TaskState.RECEIVED,
                    "budget": budget.as_json(),
                }
            )
            # 交给专家 = 这段对话从此是专家对话（原地转，同一条线）
            from app.application.workbench.session_service import SessionService

            await SessionService(self.repo).mark_expert(session)
            await self.repo.append_event(
                session_id=req.session_id,
                kind="task",
                event_type="task/received",
                payload={
                    "task_id": task_id,
                    "profile_id": req.profile_id,
                    "budget": budget.as_json(),
                    "queued_behind": queued_behind,
                },
                task_id=task_id,
            )
            if queued_behind is None:
                await self._give_wheel(session, task_id)
            else:
                await self.repo.append_event(
                    session_id=req.session_id,
                    kind="task",
                    event_type="task/queued",
                    payload={"task_id": task_id, "behind": queued_behind},
                    task_id=task_id,
                )
            if req.budget_limit is not None:
                await self.repo.ledger_append(
                    task_id=task_id,
                    attempt_id=None,
                    entry="topup",
                    amount=req.budget_limit,
                    note="initial budget",
                    actor_id=owner_actor_id,
                )
            return {
                "task_id": task_id,
                "state": TaskState.RECEIVED,
                "created": True,
                "queued_behind": queued_behind,
            }

    async def _give_wheel(self, session: dict[str, Any], task_id: str) -> None:
        """方向盘交给顾问：对话记下在跑的 Task，事件留痕。调用方持有事务与行锁。"""
        await self.repo.cas_session_wheel(
            str(session["id"]),
            expected_version=int(session["state_version"]),
            wheel=Wheel.advisor,
            active_task_id=task_id,
        )
        await self.repo.append_event(
            session_id=str(session["id"]),
            kind="wheel",
            event_type="wheel/handover",
            payload={"from": "user", "to": "advisor", "task_id": task_id},
            task_id=task_id,
        )

    async def start_next_queued(self, project_id: str) -> str | None:
        """对账用：项目里没有活跃委托却有排队的，放行最早的一件（0034 §三.2）。"""
        async with self.repo.transaction():
            if await self.repo.active_task_of_project(project_id) is not None:
                return None
            return await self._start_next_queued(project_id)

    async def settle_unattended(
        self, task_id: str, *, expected_version: int, reason: dict
    ) -> str | None:
        """没人推的委托收尾（0034 §三.2）：记着取消的收成「已取消」，否则「失败」（平台）；开着的尝试一起关。

        不重开这一步：Codex 那一轮可能还在跑，重开就是双跑。读到之后有人动过（版本变了）就不动，返回 None。
        """
        async with self.repo.transaction():
            task = await self.repo.get_task(task_id, for_update=True)
            if (
                int(task["state_version"]) != expected_version
                or TaskState(task["state"]) in TASK_TERMINAL
            ):
                return None
            cancelled = task.get("cancel_requested_at") is not None
            if task.get("active_attempt_id") is not None:
                attempt = await self.repo.get_attempt(str(task["active_attempt_id"]))
                if attempt["status"] not in ATTEMPT_TERMINAL:
                    if cancelled:
                        target = AttemptState.CANCELLED
                    elif attempt["status"] == AttemptState.SUSPENDED:
                        target = AttemptState.ABANDONED
                    else:
                        target = AttemptState.FAILED
                    await self.attempt_transition(
                        str(attempt["id"]),
                        target,
                        failure_code="cancelled" if cancelled else PLATFORM_FAILURE,
                        retryable=False,
                    )
            done = await self.transition(
                task_id,
                TaskState.CANCELLED if cancelled else TaskState.FAILED,
                reason={"by": "user"} if cancelled else reason,
            )
            return done["state"]

    async def _start_next_queued(self, project_id: str) -> str | None:
        """前一位专家到终态：排在后面的下一位开始（0017 专家团：一次一位）。调用方持有事务。"""
        nxt = await self.repo.next_queued_task(project_id)
        if nxt is None:
            return None
        session = await self.repo.get_session(str(nxt["session_id"]), for_update=True)
        if session["wheel"] != Wheel.user or session["active_task_id"] is not None:
            return None  # 这段对话正忙着别的：等下一次终态再看
        task_id = str(nxt["id"])
        original = dict(nxt["original_input"])
        context = dict(original.get("client_context") or {})
        context.pop("queued_behind", None)
        original["client_context"] = context
        await self.repo.cas_task(
            task_id,
            expected_version=int(nxt["state_version"]),
            original_input=original,
            thread_id=session.get("thread_id"),
        )
        await self._give_wheel(session, task_id)
        await self.repo.append_event(
            session_id=str(session["id"]),
            kind="task",
            event_type="task/dequeued",
            payload={"task_id": task_id},
            task_id=task_id,
        )
        await self.repo.enqueue_command(
            session_id=str(session["id"]),
            sandbox_id=str(session["sandbox_id"]),
            kind="task.drive",
            payload={"task_id": task_id},
        )
        return task_id

    # ---------------- 状态转换（唯一入口） ----------------
    async def transition(
        self,
        task_id: str,
        target: TaskState | str,
        *,
        expected_version: int | None = None,
        waiting_reason: WaitingReason | str | None = None,
        reason: dict | None = None,
        actor_id: str | None = None,
        **extra_fields: Any,
    ) -> dict[str, Any]:
        target = TaskState(target)
        async with self.repo.transaction():
            task = await self.repo.get_task(task_id, for_update=True)
            validate_task_transition(
                task["state"],
                target,
                waiting_reason=str(waiting_reason) if waiting_reason else None,
            )
            version = (
                int(task["state_version"])
                if expected_version is None
                else expected_version
            )
            fields: dict[str, Any] = {
                "state": target,
                "waiting_reason": str(waiting_reason) if waiting_reason else None,
                **extra_fields,
            }
            if target in TASK_TERMINAL:
                fields["active_attempt_id"] = None
                fields["active_interaction_id"] = None
                if target is TaskState.REJECTED:
                    fields["rejection"] = reason or {}
            new_version = await self.repo.cas_task(
                task_id, expected_version=version, **fields
            )
            payload = {
                "task_id": task_id,
                "from": task["state"],
                "to": target,
                "waiting_reason": fields["waiting_reason"],
                "reason": reason or {},
            }
            await self.repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="task/state",
                payload=payload,
                task_id=task_id,
            )
            wheel = wheel_after_task_state(target)
            if wheel is not None:
                session = await self.repo.get_session(
                    str(task["session_id"]), for_update=True
                )
                if (
                    session["active_task_id"] is not None
                    and str(session["active_task_id"]) == task_id
                ):
                    await self.repo.cas_session_wheel(
                        str(task["session_id"]),
                        expected_version=int(session["state_version"]),
                        wheel=wheel,
                        active_task_id=None,
                    )
                    await self.repo.append_event(
                        session_id=str(task["session_id"]),
                        kind="wheel",
                        event_type="wheel/return",
                        payload={
                            "from": "advisor",
                            "to": "user",
                            "task_id": task_id,
                            "final_state": target,
                            # 对话里交回那一行要说清为什么停：打回是根本没开工，不能叫人去看底稿
                            "reason_text": reason_text(target, reason),
                            # 这位专家写不写成稿文件：不写的（只回结构化结果的分身）不叫人去「工作底稿」看
                            # （qwen-005 走查：交回行指向右栏底稿，右栏却是空的）
                            "writes_files": writes_files(task),
                        },
                        task_id=task_id,
                    )
                await self.repo.cancel_pending_interactions(
                    task_id=task_id, reason=f"task {target}"
                )
            if target in TASK_TERMINAL and task.get("project_id"):
                await self._start_next_queued(str(task["project_id"]))
            return {"task_id": task_id, "state": target, "state_version": new_version}

    async def resume_environment(
        self, task_id: str, *, session_id: str, environment_id: str
    ) -> bool:
        """runner 已核对执行环境 ready。恢复与排队同一事务，重复/取消竞争只生效一次。"""
        async with self.repo.transaction():
            task = await self.repo.get_task(task_id, for_update=True)
            if (
                str(task["session_id"]) != session_id
                or task["state"] != TaskState.WAITING
                or task.get("waiting_reason") != WaitingReason.ENVIRONMENT
                or task.get("cancel_requested_at") is not None
                or task.get("active_attempt_id") is not None
                or task.get("active_interaction_id") is not None
            ):
                return False
            session = await self.repo.get_session(session_id)
            if (
                str(session.get("environment_id")) != environment_id
                or str(session.get("active_task_id")) != task_id
                or session["wheel"] != Wheel.advisor
            ):
                return False
            environment = await self.repo.get_environment(
                environment_id, owner_actor_id=str(session["owner_actor_id"])
            )
            if environment.get("status") != "online":
                return False
            await self.transition(
                task_id,
                TaskState.QUEUED,
                expected_version=int(task["state_version"]),
                reason={
                    "environment_id": environment_id,
                    "execution_environment": "ready",
                },
            )
            await self.repo.enqueue_command(
                session_id=session_id,
                sandbox_id=str(session["sandbox_id"]),
                kind="task.drive",
                payload={"task_id": task_id, "resumed": True},
            )
            return True

    async def request_cancel(
        self, task_id: str, *, owner_actor_id: str
    ) -> dict[str, Any]:
        """取消意图先持久化（AT-13），同一事务里直接收成「已取消」，不等推它的程序来落实（0034 §三.1）。

        在跑的：开着的那一轮尝试一起关掉（这时不知道花了多少，记 0、预留全退），调用方照发一次中断；
        推它的程序若还活着，这一轮跑完撞上终态就只补记花费（`record_late_cost`），安静停下。
        """
        async with self.repo.transaction():
            task = await self.repo.get_task(
                task_id, owner_actor_id=owner_actor_id, for_update=True
            )
            if TaskState(task["state"]) in TASK_TERMINAL:
                return {
                    "task_id": task_id,
                    "state": task["state"],
                    "cancel_requested": False,
                }
            version = await self.repo.cas_task(
                task_id,
                expected_version=int(task["state_version"]),
                cancel_requested_at=datetime.now(UTC),
                cancel_requested_by=owner_actor_id,
            )
            await self.repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="task/cancel_requested",
                payload={"task_id": task_id},
                task_id=task_id,
            )
            running = TaskState(task["state"]) is TaskState.RUNNING
            if running and task.get("active_attempt_id") is not None:
                attempt = await self.repo.get_attempt(str(task["active_attempt_id"]))
                if attempt["status"] not in ATTEMPT_TERMINAL:
                    await self.repo.flush()
                    await self.attempt_transition(
                        str(attempt["id"]),
                        AttemptState.CANCELLED,
                        failure_code="cancelled",
                    )
                    version = int((await self.repo.get_task(task_id))["state_version"])
            # WAITING 时那一轮可能还卡在等人批准、等人回答，在跑的那一轮还在 Codex 里：
            # 都要调用方照样发一次中断把它收住（interrupt），不然 Codex 要等到时限
            done = await self.transition(
                task_id,
                TaskState.CANCELLED,
                expected_version=version,
                reason={"by": "user"},
            )
            return {
                **done,
                "cancel_requested": True,
                "interrupt": running or TaskState(task["state"]) is TaskState.WAITING,
            }

    async def record_late_cost(
        self,
        task_id: str,
        *,
        attempt_id: str,
        consumed: Decimal,
        tokens: dict | None,
        turn_id: str | None,
    ) -> None:
        """委托已被别人收尾（取消、测试超时、对账）之后，推它的程序这一轮才跑完：花掉的钱补记上（0034 §三.1）。

        尝试已经关掉、预留已经退了：花费加进委托的 `budget.used` 和这一轮尝试的花费（步骤上看得到这一步花了
        多少），记一笔 consume 和一条事件；不碰预留，不改尝试的状态。
        """
        if not consumed:
            return
        async with self.repo.transaction():
            task = await self.repo.get_task(task_id, for_update=True)
            attempt = await self.repo.get_attempt(attempt_id, for_update=True)
            await self.repo.update_attempt(
                attempt_id,
                budget_consumed={
                    "amount": str(
                        Decimal(str(attempt["budget_consumed"].get("amount", "0")))
                        + consumed
                    ),
                    "tokens": tokens or {},
                },
                **({"turn_ids": [*attempt["turn_ids"], turn_id]} if turn_id else {}),
            )
            budget = _budget(task)
            budget.used += consumed
            await self.repo.cas_task(
                task_id,
                expected_version=int(task["state_version"]),
                budget=budget.as_json(),
            )
            await self.repo.ledger_append(
                task_id=task_id,
                attempt_id=attempt_id,
                entry="consume",
                amount=consumed,
                tokens=tokens or {},
            )
            await self.repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="attempt/late_cost",
                payload={
                    "attempt_id": attempt_id,
                    "consumed": str(consumed),
                    "turn_id": turn_id,
                },
                task_id=task_id,
                attempt_id=attempt_id,
            )

    # ---------------- Attempt ----------------
    async def open_attempt(
        self,
        task_id: str,
        *,
        step_id: str | None,
        step_version: str | None,
        role: AttemptRole = AttemptRole.execute,
        input_artifact_versions: list | None = None,
        reserve: Decimal = Decimal("0"),
        meta: dict | None = None,
    ) -> dict[str, Any]:
        async with self.repo.transaction():
            task = await self.repo.get_task(task_id, for_update=True)
            if task["active_attempt_id"] is not None:
                raise StaleStateVersion(
                    "task already has an active attempt (phase 1 forbids parallel attempts)",
                    task_id=task_id,
                )
            budget = _budget(task)
            if reserve > budget.available:
                raise BudgetExhausted(
                    "insufficient budget to open an attempt",
                    task_id=task_id,
                    available=str(budget.available),
                )
            attempt_id = str(uuid.uuid4())
            await self.repo.insert_attempt(
                {
                    "id": attempt_id,
                    "task_id": task_id,
                    "session_id": str(task["session_id"]),
                    "thread_id": task["thread_id"],
                    "environment_id": str(task["environment_id"]),
                    "role": role,
                    "arm": 0,
                    "step_id": step_id,
                    "step_version": step_version,
                    "input_artifact_versions": input_artifact_versions or [],
                    "turn_ids": [],
                    "status": AttemptState.CREATED,
                    "budget_allocated": {"amount": str(reserve)},
                    "budget_consumed": {},
                    "output_artifacts": [],
                    "refs": meta or {},
                    "codex_version": (meta or {}).get("codex_version"),
                    "agent_version": (meta or {}).get("agent_version"),
                    "model": (meta or {}).get("model"),
                    "model_provider": (meta or {}).get("model_provider"),
                }
            )
            budget.reserved += reserve
            fields: dict[str, Any] = {
                "active_attempt_id": attempt_id,
                "budget": budget.as_json(),
            }
            if TaskState(task["state"]) is TaskState.QUEUED:
                validate_task_transition(task["state"], TaskState.RUNNING)
                fields["state"] = TaskState.RUNNING
                fields["waiting_reason"] = None
            await self.repo.cas_task(
                task_id, expected_version=int(task["state_version"]), **fields
            )
            if reserve:
                await self.repo.ledger_append(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    entry="reserve",
                    amount=reserve,
                )
            await self.repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="attempt/opened",
                payload={"attempt_id": attempt_id, "step_id": step_id, "role": role},
                task_id=task_id,
                attempt_id=attempt_id,
            )
            if "state" in fields:
                await self.repo.append_event(
                    session_id=str(task["session_id"]),
                    kind="task",
                    event_type="task/state",
                    payload={
                        "task_id": task_id,
                        "from": task["state"],
                        "to": TaskState.RUNNING,
                    },
                    task_id=task_id,
                )
            return {"attempt_id": attempt_id, "task_id": task_id}

    async def attempt_transition(
        self,
        attempt_id: str,
        target: AttemptState | str,
        *,
        consumed: Decimal = Decimal("0"),
        tokens: dict | None = None,
        failure_code: str | None = None,
        retryable: bool | None = None,
        turn_id: str | None = None,
        output_artifacts: list | None = None,
    ) -> dict[str, Any]:
        target = AttemptState(target)
        async with self.repo.transaction():
            attempt = await self.repo.get_attempt(attempt_id, for_update=True)
            validate_attempt_transition(attempt["status"], target)
            task = await self.repo.get_task(str(attempt["task_id"]), for_update=True)
            fields: dict[str, Any] = {"status": target}
            if turn_id:
                fields["turn_ids"] = [*attempt["turn_ids"], turn_id]
            if target is AttemptState.RUNNING and attempt["started_at"] is None:
                fields["started_at"] = datetime.now(UTC)
            if target in ATTEMPT_TERMINAL:
                fields["ended_at"] = datetime.now(UTC)
                fields["failure_code"] = failure_code
                fields["retryable"] = retryable
                if output_artifacts is not None:
                    fields["output_artifacts"] = output_artifacts
            budget = _budget(task)
            task_fields: dict[str, Any] = {}
            if consumed:
                reserved = Decimal(str(attempt["budget_allocated"].get("amount", "0")))
                budget.used += consumed
                budget.reserved = max(
                    Decimal("0"), budget.reserved - min(consumed, reserved)
                )
                fields["budget_consumed"] = {
                    "amount": str(
                        Decimal(str(attempt["budget_consumed"].get("amount", "0")))
                        + consumed
                    ),
                    "tokens": tokens or {},
                }
                await self.repo.ledger_append(
                    task_id=str(task["id"]),
                    attempt_id=attempt_id,
                    entry="consume",
                    amount=consumed,
                    tokens=tokens or {},
                )
            if target in ATTEMPT_TERMINAL:
                leftover = (
                    Decimal(str(attempt["budget_allocated"].get("amount", "0")))
                    - consumed
                )
                if leftover > 0 and budget.reserved > 0:
                    rel = min(leftover, budget.reserved)
                    budget.reserved -= rel
                    await self.repo.ledger_append(
                        task_id=str(task["id"]),
                        attempt_id=attempt_id,
                        entry="release",
                        amount=rel,
                    )
                task_fields["active_attempt_id"] = None
            task_fields["budget"] = budget.as_json()
            await self.repo.update_attempt(attempt_id, **fields)
            version = await self.repo.cas_task(
                str(task["id"]),
                expected_version=int(task["state_version"]),
                **task_fields,
            )
            await self.repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="attempt/state",
                payload={
                    "attempt_id": attempt_id,
                    "from": attempt["status"],
                    "to": target,
                    "failure_code": failure_code,
                    "consumed": str(consumed),
                },
                task_id=str(task["id"]),
                attempt_id=attempt_id,
            )
            # 预算硬停：Attempt 超限 → Task WAITING(RESOURCE)，追加须用户批准（C-A10、AT-12）
            if target is AttemptState.BUDGET_EXCEEDED:
                await self.repo.flush()
                await self._enter_waiting_resource(str(task["id"]), version)
            return {
                "attempt_id": attempt_id,
                "status": target,
                "task_state_version": version,
            }

    async def _enter_waiting_resource(self, task_id: str, version: int) -> None:
        task = await self.repo.get_task(task_id, for_update=True)
        validate_task_transition(
            task["state"], TaskState.WAITING, waiting_reason=WaitingReason.RESOURCE
        )
        token = secrets.token_urlsafe(32)
        iid = await self.repo.insert_interaction(
            session_id=str(task["session_id"]),
            task_id=task_id,
            attempt_id=None,
            kind="resource",
            prompt=InteractionPrompt(
                title="预算已用尽",
                question="是否追加预算继续？",
                options=[
                    {"id": "topup", "label": "追加"},
                    {"id": "stop", "label": "停止"},
                ],
                subject={"budget": task["budget"]},
            ).model_dump(),
            subject_digest=None,
            token=token,
            target_state_version=version + 1,
            expires_at=datetime.now(UTC) + INTERACTION_TTL,
        )
        await self.repo.cas_task(
            task_id,
            expected_version=version,
            state=TaskState.WAITING,
            waiting_reason=WaitingReason.RESOURCE,
            active_interaction_id=iid,
        )
        await self.repo.append_event(
            session_id=str(task["session_id"]),
            kind="task",
            event_type="task/state",
            payload={
                "task_id": task_id,
                "from": task["state"],
                "to": TaskState.WAITING,
                "waiting_reason": WaitingReason.RESOURCE,
            },
            task_id=task_id,
        )
        await self.repo.append_event(
            session_id=str(task["session_id"]),
            kind="interaction",
            event_type="interaction/opened",
            payload={"interaction_id": iid, "kind": "resource", "token": token},
            task_id=task_id,
        )

    # ---------------- Interaction ----------------
    async def open_interaction(
        self,
        task_id: str,
        *,
        kind: str,
        prompt: InteractionPrompt,
        waiting_reason: WaitingReason,
        attempt_id: str | None = None,
        subject_digest: str | None = None,
        ttl: timedelta = INTERACTION_TTL,
    ) -> dict[str, Any]:
        """Task 进 WAITING 与创建 Interaction 同一提交（state-machine.md「WAITING 与 Interaction」）。令牌只在事件里出现一次，表里只存哈希。"""
        async with self.repo.transaction():
            task = await self.repo.get_task(task_id, for_update=True)
            validate_task_transition(
                task["state"], TaskState.WAITING, waiting_reason=waiting_reason
            )
            token = secrets.token_urlsafe(32)
            version = int(task["state_version"])
            iid = await self.repo.insert_interaction(
                session_id=str(task["session_id"]),
                task_id=task_id,
                attempt_id=attempt_id,
                kind=kind,
                prompt=prompt.model_dump(),
                subject_digest=subject_digest,
                token=token,
                target_state_version=version + 1,
                expires_at=datetime.now(UTC) + ttl,
            )
            await self.repo.cas_task(
                task_id,
                expected_version=version,
                state=TaskState.WAITING,
                waiting_reason=waiting_reason,
                active_interaction_id=iid,
            )
            await self.repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="task/state",
                payload={
                    "task_id": task_id,
                    "from": task["state"],
                    "to": TaskState.WAITING,
                    "waiting_reason": waiting_reason,
                },
                task_id=task_id,
            )
            ev = await self.repo.append_event(
                session_id=str(task["session_id"]),
                kind="interaction",
                event_type="interaction/opened",
                payload={
                    "interaction_id": iid,
                    "kind": kind,
                    "token": token,
                    "prompt": prompt.model_dump(),
                    # 网页的时间线按 subject 画审批卡；放一份在顶层，和自驾那条路一样
                    "subject": prompt.subject,
                },
                task_id=task_id,
                attempt_id=attempt_id,
            )
            return {"interaction_id": iid, "token": token, "event": ev}

    async def respond_interaction(
        self,
        interaction_id: str,
        *,
        token: str,
        response: dict[str, Any],
        owner_actor_id: str,
        subject_digest: str | None = None,
        resume_to: TaskState = TaskState.QUEUED,
    ) -> dict[str, Any]:
        """原子消费（AT-07）：重复、过期、异键、跨 Task、摘要不符一律拒绝；成功后 Task 回 QUEUED（或 VALIDATING）。"""
        async with self.repo.transaction():
            it = await self.repo.get_interaction(interaction_id, for_update=True)
            session = await self.repo.get_session(
                str(it["session_id"]), owner_actor_id=owner_actor_id
            )
            if token_hash(token) != it["token_hash"]:
                raise InteractionRejected(
                    "token mismatch", interaction_id=interaction_id
                )
            if (
                it["subject_digest"] is not None
                and subject_digest != it["subject_digest"]
            ):
                raise InteractionRejected(
                    "subject digest mismatch", interaction_id=interaction_id
                )
            if not await self.repo.consume_interaction(
                interaction_id, response=response, responded_by=owner_actor_id
            ):
                raise InteractionRejected(
                    "interaction already consumed, expired or cancelled",
                    interaction_id=interaction_id,
                )
            result: dict[str, Any] = {
                "interaction_id": interaction_id,
                "session_id": str(session["id"]),
                "consumed": True,
            }
            if it["task_id"] is not None:
                task = await self.repo.get_task(str(it["task_id"]), for_update=True)
                if (
                    task["active_interaction_id"] is None
                    or str(task["active_interaction_id"]) != interaction_id
                ):
                    raise InteractionRejected(
                        "interaction does not belong to the task's current wait",
                        interaction_id=interaction_id,
                    )
                if it["target_state_version"] is not None and int(
                    task["state_version"]
                ) != int(it["target_state_version"]):
                    raise InteractionRejected(
                        "task moved on since this interaction was opened",
                        interaction_id=interaction_id,
                    )
                await self.repo.append_event(
                    session_id=str(task["session_id"]),
                    kind="interaction",
                    event_type="interaction/consumed",
                    payload={
                        "interaction_id": interaction_id,
                        "kind": it["kind"],
                        "response": response,
                    },
                    task_id=str(task["id"]),
                )
                if it["kind"] == "resource":
                    if response.get("decision") == "topup" and response.get("amount"):
                        budget = _budget(task)
                        budget.limit = (budget.limit or Decimal("0")) + Decimal(
                            str(response["amount"])
                        )
                        await self.repo.cas_task(
                            str(task["id"]),
                            expected_version=int(task["state_version"]),
                            budget=budget.as_json(),
                            active_interaction_id=None,
                        )
                        await self.repo.ledger_append(
                            task_id=str(task["id"]),
                            attempt_id=None,
                            entry="topup",
                            amount=Decimal(str(response["amount"])),
                            actor_id=owner_actor_id,
                        )
                        result.update(
                            await self.transition(
                                str(task["id"]),
                                TaskState.QUEUED,
                                reason={"topup": str(response["amount"])},
                            )
                        )
                    else:
                        await self.repo.cas_task(
                            str(task["id"]),
                            expected_version=int(task["state_version"]),
                            active_interaction_id=None,
                        )
                        result.update(
                            await self.transition(
                                str(task["id"]),
                                TaskState.FAILED,
                                reason={"budget": "user declined top-up"},
                            )
                        )
                else:
                    await self.repo.cas_task(
                        str(task["id"]),
                        expected_version=int(task["state_version"]),
                        active_interaction_id=None,
                    )
                    if response.get("decision") == "stop" and (
                        (it.get("prompt") or {}).get("subject") or {}
                    ).get("out_of_scope"):
                        # 专家说不归它、用户也说不做了：这件委托是取消，不是失败（账 130）
                        result.update(
                            await self.transition(
                                str(task["id"]),
                                TaskState.CANCELLED,
                                reason={
                                    "interaction": interaction_id,
                                    "by": "user",
                                    "code": "out_of_scope",
                                },
                            )
                        )
                    elif response.get("decision") == "stop":
                        result.update(
                            await self.transition(
                                str(task["id"]),
                                TaskState.FAILED,
                                reason={
                                    "interaction": interaction_id,
                                    "by": "user stopped",
                                },
                            )
                        )
                    else:
                        result.update(
                            await self.transition(
                                str(task["id"]),
                                resume_to,
                                reason={"interaction": interaction_id},
                            )
                        )
            else:
                await self.repo.append_event(
                    session_id=str(session["id"]),
                    kind="interaction",
                    event_type="interaction/consumed",
                    payload={
                        "interaction_id": interaction_id,
                        "kind": it["kind"],
                        "response": response,
                    },
                )
            return result

    # ---------------- 查询 ----------------
    async def task_view(self, task_id: str, *, owner_actor_id: str) -> dict[str, Any]:
        task = await self.repo.get_task(task_id, owner_actor_id=owner_actor_id)
        attempts = await self.repo.list_attempts(task_id)
        artifacts = await self.repo.list_artifacts(task_id)
        for d in (task, *attempts, *artifacts):
            for k, v in list(d.items()):
                if hasattr(v, "hex") and not isinstance(v, (bytes, str)):
                    d[k] = str(v)
        return {"task": task, "attempts": attempts, "artifacts": artifacts}
