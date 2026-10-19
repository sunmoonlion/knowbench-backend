"""顾问：表驱动的工作流解释器（methods.md「编排就是逐步发 turn」）。不调模型；对用户自己的 Codex thread 逐步发 turn。

一步的循环：开 Attempt（预留预算）→ 组 turn 输入（方法文本 + 上游 Artifact + 输出说明）→ turn/start → 等 turn/completed →
按预算账扣 token → 确定性验收 → 通过：落新版本 Artifact、游标 +1；不通过：按 on_reject 返工 / 回退 / 交人。
崩了重启就从游标与 Artifact 版本续：这里没有任何内存里的必需状态。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.application.ports.workbench import (
    KnowbotDataSource,
    WorkbenchStore,
    WorkbenchStores,
)
from app.application.workbench.acceptance import judge, out_of_scope, timed_out
from app.application.workbench.acceptance_numbers import judge_numbers
from app.application.workbench.catalog import SNAPSHOT_SOURCES, pack_for_task
from app.application.workbench.deliverable_check import DeliverableCheck, harden
from app.application.workbench.ledger import Ledger
from app.domain.workbench import knowbots, missing_data
from app.domain.workbench.deliverables import (
    DELIVERABLE_RULES,
    HANDBACK_FILE,
    WRITE_FILE_RULES,
    deliverable_seen,
    handback_rules,
    placed,
    run_folder,
)
from app.domain.workbench.errors import BudgetExhausted
from app.domain.workbench.memory import memory_note
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.packs import ExpertPack, Scope, StepContract, find_pack
from app.domain.workbench.projects import NO_DETOUR
from app.domain.workbench.records import one_line
from app.domain.workbench.states import (
    ATTEMPT_TERMINAL,
    PLATFORM_FAILURE,
    TASK_TERMINAL,
    AttemptState,
    InvalidTransition,
    TaskState,
    WaitingReason,
)
from app.domain.workbench.step_view import RAW_CHARS

log = logging.getLogger(__name__)

# 专家每一步的时限（一个 turn）。到点还没做完就停下，按验收不过走返工、退回或交人
STEP_TIMEOUT_SECONDS = 900
# 重做时列出上一次没过的几条，每条最多这么多字
REWORK_FAILURES_SHOWN = 10
REWORK_FAILURE_CHARS = 400


@dataclass
class TurnResult:
    turn_id: str | None
    final_text: str | None
    tokens: dict[str, Any]  # 这一轮各次调用相加的用量（pricing.Usage.as_json）
    error: str | None = None
    environment_lost: bool = False
    # 这一轮花了多少（估算）。不认识这个模型的单价时是 None：只有用量，没有金额
    cost: Decimal | None = None
    interrupted: bool = False
    # 用户在审批面点了「停下」（或等不到人）：这一轮是被人停的，不是出错
    stopped_by_user: bool = False
    stop_note: str | None = None
    # 这一轮用补丁改到的文件（绝对路径）；成稿文件写了没有就看它（0015 §3.3）
    files: list[str] = field(default_factory=list)
    # 这一步到了时限还没做完，被工作台停下：最后一句只是进度说明，不是交回物，不拿去验
    timed_out: bool = False


class TurnDriver(Protocol):
    """runner 的 SandboxLink 实现它：在给定 thread 上发一个 turn 并等它结束。"""

    async def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        timeout: float = STEP_TIMEOUT_SECONDS,  # noqa: ASYNC109
    ) -> TurnResult: ...


class Advisor:
    def __init__(
        self,
        stores: WorkbenchStores,
        driver: TurnDriver,
        *,
        records: bool = False,
        deliverables: DeliverableCheck | None = None,
        data: KnowbotDataSource | None = None,
    ):
        self.stores = stores  # 每次调用开一个工作单元
        self.driver = driver
        # 沙箱里配好了读项目记录的工具：每一步的说明里告诉模型有这组工具、委托编号是什么
        self.records = records
        # 成稿硬检查（0022 段 4）：没配就只有软检查
        self.deliverables = deliverables
        # 数值验收 sourced 要重新取用户的数（0030 §3.3）：没配就判不过，不放行
        self.data = data

    # ---------------- 入口 ----------------
    async def drive(self, task_id: str) -> str:
        """把一个 Task 推到停下为止：终态，或 WAITING（等人 / 等预算 / 等环境）。返回停下时的状态。"""
        async with self.stores() as repo:
            led = Ledger(repo)
            task = await repo.get_task(task_id)
            if task["state"] == TaskState.RECEIVED:
                await led.transition(task_id, TaskState.VALIDATING)
                task = await repo.get_task(task_id)
            if task["state"] == TaskState.VALIDATING:
                pack = pack_for_task(task)
                if pack is None:
                    # 专家还在、只是委托绑的那一版已经下架（升版前建的单）：说清楚，别说成不在范围。
                    # 分身的委托自带快照，不会走到这里
                    gone = (
                        task.get("pack_source") not in SNAPSHOT_SOURCES
                        and find_pack(task["profile_id"]) is not None
                    )
                    await led.transition(
                        task_id,
                        TaskState.REJECTED,
                        reason={
                            "code": "pack_version_gone" if gone else "no_expert_pack",
                            "message": "这个委托按专家的旧版建的，旧版已经换下，请重新请一次专家"
                            if gone
                            else "这个问题不在专家范围，继续自己用 Codex 即可",
                            "version": task["profile_version"],
                        },
                    )
                    return TaskState.REJECTED
                if not task["thread_id"]:
                    await led.transition(
                        task_id,
                        TaskState.REJECTED,
                        reason={
                            "code": "no_thread",
                            "message": "会话还没有 Codex thread",
                        },
                    )
                    return TaskState.REJECTED
                await repo.cas_task(
                    task_id,
                    expected_version=(await repo.get_task(task_id))["state_version"],
                    workflow_version=pack.version,
                    expert_pack_version=pack.version,
                    acceptance_contract={
                        "pack_id": pack.pack_id,
                        "steps": [st.step_id for st in pack.workflow],
                    },
                )
                await repo.commit()
                await led.transition(task_id, TaskState.QUEUED)
        while True:
            state = await self._step_once(task_id)
            if state != TaskState.QUEUED:
                return state

    # ---------------- 一步 ----------------
    async def _step_once(self, task_id: str) -> str:
        async with self.stores() as repo:
            led = Ledger(repo)
            task = await repo.get_task(task_id)
            if task["state"] not in (TaskState.QUEUED, TaskState.RUNNING):
                return task["state"]
            if task["cancel_requested_at"] is not None:
                return (
                    await led.transition(
                        task_id, TaskState.CANCELLED, reason={"by": "user"}
                    )
                )["state"]
            pack = pack_for_task(task)
            assert pack is not None
            step = pack.step(int(task["current_step"]))
            if step is None:
                return await self._finish(led, repo, task, pack)
            attempts = await repo.list_attempts(task_id)
            reworks = sum(
                1
                for a in attempts
                if a["step_id"] == step.step_id
                and a["status"] == AttemptState.FAILED
                and a["failure_code"] == "acceptance"
            )
            inputs = {}
            for name in step.input_refs:
                art = await repo.get_artifact(task_id=task_id, name=name)
                if art is None:
                    return (
                        await led.transition(
                            task_id,
                            TaskState.FAILED,
                            reason={
                                "code": "missing_input",
                                "step": step.step_id,
                                "artifact": name,
                            },
                        )
                    )["state"]
                inputs[name] = {"version": art["version"], "content": art["content"]}
            try:
                opened = await led.open_attempt(
                    task_id,
                    step_id=step.step_id,
                    step_version=step.step_version,
                    input_artifact_versions=[
                        {"name": k, "version": v["version"]} for k, v in inputs.items()
                    ],
                    reserve=Decimal(step.reserve),
                    meta={"reworks": reworks},
                )
            except BudgetExhausted as exc:
                log.info("budget exhausted before step %s: %s", step.step_id, exc)
                return await self._hard_stop(led, repo, task_id)
            attempt_id = opened["attempt_id"]
            await repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="step/started",
                payload={
                    "step_id": step.step_id,
                    "title": step.title,
                    "attempt_id": attempt_id,
                    "rework": reworks,
                },
                task_id=task_id,
                attempt_id=attempt_id,
            )
            await repo.commit()
            thread_id = task["thread_id"]
            question = task["original_input"].get("input", {})
            # 本步之前用户答过的：开步前的问题（ask_before）、停下时附的要求、模型问过的
            answered = await self._answers_for_step(repo, task, step)
            if step.ask_before and answered["ask_before"] is None:
                # 工作台发起的澄清：开步前一次问完（approval.md「Task 级：澄清·工作台发起」）
                await led.attempt_transition(
                    attempt_id, AttemptState.CANCELLED, failure_code="ask_before"
                )
                await repo.commit()
                it = await led.open_interaction(
                    task_id,
                    kind="question",
                    prompt=InteractionPrompt(
                        title=f"开始「{step.title}」之前，专家想先问你",
                        question=step.ask_before[0].question
                        if len(step.ask_before) == 1
                        else f"{len(step.ask_before)} 个问题",
                        options=[],
                        subject={
                            "step_id": step.step_id,
                            "ask_before": True,
                            "questions": [
                                {
                                    "id": q.id,
                                    "header": "",
                                    "question": q.question,
                                    "options": [
                                        {
                                            "label": o.get("label", ""),
                                            "description": "",
                                            "id": o.get("id", ""),
                                        }
                                        for o in q.options
                                    ],
                                    "is_other": q.allow_other,
                                    "is_secret": False,
                                }
                                for q in step.ask_before
                            ],
                        },
                    ),
                    waiting_reason=WaitingReason.INPUT,
                )
                log.info(
                    "step %s asks before start interaction=%s",
                    step.step_id,
                    it["interaction_id"],
                )
                return TaskState.WAITING
            # 受理关口（账 130）：第一步先判问题归不归这位专家。做过一步的、用户说了照做的，不再判
            intake = (
                pack.intake_scope()
                if step.step_id == pack.workflow[0].step_id
                and not answered["proceed"]
                and not any(a["status"] == AttemptState.COMPLETED for a in attempts)
                else None
            )
            # 这一次委托的底稿放在自己那一格，不覆盖别的对话与前几次（所有者 2026-10-14）
            folder = run_folder(
                str(task["session_id"]), task["created_at"], pack.name or pack.title
            )
            # 重做：把上一次没过的各条告诉专家（所有者 2026-10-17 定），不然它不知道该改哪里
            rejected = (
                [
                    e["payload"]
                    for e in await repo.list_task_events(
                        task_id, types=("step/rejected",)
                    )
                    if e["payload"].get("step_id") == step.step_id
                ]
                if reworks
                else []
            )
            text = self.compose(
                step,
                question,
                inputs,
                reworks,
                rework_failures=rejected[-1].get("failures") if rejected else None,
                folder=folder,
                records_task=task_id if self.records else None,
                data_task=(
                    task_id
                    if self.records
                    and task.get("pack_source") in SNAPSHOT_SOURCES
                    and knowbots.DATA_TOOLS & set(step.tools)
                    else None
                ),
                ask_before_answers=answered["ask_before"],
                change_request=answered["change_request"],
                user_facts=memory_note(
                    await repo.list_memories(
                        str(task["owner_actor_id"]), enabled_only=True
                    )
                ),
                last_step=pack.workflow[-1].step_id == step.step_id,
                intake=intake,
            )

        result = await self.driver.run_turn(
            thread_id, text, timeout=STEP_TIMEOUT_SECONDS
        )

        async with self.stores() as repo:
            led = Ledger(repo)
            consumed = result.cost or Decimal("0")
            # 这一轮结束了：它里面还挂着的问题作废、委托回到在跑，下面才按结果走得下去
            await led.end_turn_waits(task_id)
            task = await repo.get_task(task_id)
            settled = (await repo.get_attempt(attempt_id))["status"] in ATTEMPT_TERMINAL
            if not settled:
                try:
                    await led.attempt_transition(
                        attempt_id, AttemptState.RUNNING, turn_id=result.turn_id
                    )
                except InvalidTransition:
                    # 读完到改之间被取消插了进来：同样当作别人已经收了尾
                    settled = (await repo.get_attempt(attempt_id))[
                        "status"
                    ] in ATTEMPT_TERMINAL
                    if not settled:
                        raise
            if settled:
                # 这一轮跑着的时候委托已被别人收尾（取消、测试超时、对账；0034 §三.1），尝试也已关掉：
                # 花掉的钱补记到委托上（按这一轮的编号只记一次）；这一轮结束了，放开项目，安静停下
                await led.record_late_cost(
                    task_id,
                    attempt_id=attempt_id,
                    consumed=consumed,
                    tokens=result.tokens,
                    turn_id=result.turn_id,
                )
                await led.release_turn(task_id)
                return (await repo.get_task(task_id))["state"]
            task = await repo.get_task(task_id)
            if task["cancel_requested_at"] is not None:
                # 用户点了停止：这一步被中断。已经花的照记，做完的部分交回
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.CANCELLED,
                    consumed=consumed,
                    tokens=result.tokens,
                    failure_code="cancelled",
                )
                if task["state"] in TASK_TERMINAL:
                    # 停在等人批准、等人回答时取消的：委托已经收成「已取消」，这里只把这一步记完
                    return task["state"]
                return (
                    await led.transition(
                        task_id, TaskState.CANCELLED, reason={"by": "user"}
                    )
                )["state"]
            if result.stopped_by_user:
                # 用户在审批面点了「停下」或等不到人：这一步停在这里等用户决定（approval.md：WAITING(USER)，
                # 这里用现成的 input 待办表达：带着要求重做，或停止）
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.FAILED,
                    consumed=consumed,
                    tokens=result.tokens,
                    failure_code="stopped",
                    retryable=True,
                )
                await repo.append_event(
                    session_id=str(task["session_id"]),
                    kind="task",
                    event_type="step/stopped",
                    payload={
                        "step_id": step.step_id,
                        "note": result.stop_note,
                        "by": "user",
                    },
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
                await repo.commit()
                it = await led.open_interaction(
                    task_id,
                    kind="input",
                    prompt=InteractionPrompt(
                        title=f"你停下了第「{step.title}」步",
                        question="带着你的要求重做这一步，还是停止？"
                        if result.stop_note
                        else "重做这一步，还是停止？",
                        options=[
                            {"id": "rework", "label": "重做这一步"},
                            {"id": "stop", "label": "停止"},
                        ],
                        subject={
                            "step_id": step.step_id,
                            "stopped": True,
                            "note": result.stop_note,
                            "last_output": (result.final_text or "")[:2000],
                        },
                    ),
                    waiting_reason=WaitingReason.INPUT,
                    attempt_id=attempt_id,
                )
                log.info(
                    "step %s stopped by user interaction=%s",
                    step.step_id,
                    it["interaction_id"],
                )
                return TaskState.WAITING
            if result.environment_lost or (
                result.error and not result.final_text and not result.timed_out
            ):
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.FAILED,
                    consumed=consumed,
                    tokens=result.tokens,
                    failure_code="environment"
                    if result.environment_lost
                    else "turn_error",
                    retryable=True,
                )
                reason = WaitingReason.ENVIRONMENT if result.environment_lost else None
                if reason:
                    return (
                        await led.transition(
                            task_id,
                            TaskState.WAITING,
                            waiting_reason=reason,
                            reason={"error": result.error},
                        )
                    )["state"]
                return (
                    await led.transition(
                        task_id, TaskState.FAILED, reason={"error": result.error}
                    )
                )["state"]
            declined = out_of_scope(result.final_text) if intake else None
            if declined is not None:
                return await self._out_of_scope(
                    led, repo, task, pack, step, attempt_id, result, declined
                )
            budget = task["budget"]
            # 用户给了上限才有「超了」这回事。不给上限的，花多少记多少，停不停由用户看着定
            limit = budget.get("limit")
            if limit is not None and consumed > Decimal(str(limit)) - Decimal(
                str(budget["used"])
            ):
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.BUDGET_EXCEEDED,
                    consumed=consumed,
                    tokens=result.tokens,
                )
                return TaskState.WAITING
            verdict = (
                timed_out(STEP_TIMEOUT_SECONDS // 60, step.acceptance)
                if result.timed_out
                else await judge_numbers(
                    judge(result.final_text, step.acceptance),
                    step.acceptance,
                    repo=repo,
                    task=task,
                    source=self.data,
                )
            )
            # 成稿文件写了没有：先认这一轮报过的 fileChange；没见到就经会合点问代理那个文件在不在
            # （0022 段 4）：在 → 认；明确不在 → 验收不过；问不到 → 只记事件、不填路径（软检查）
            written = None
            output_file = placed(step.output_file, folder) if step.output_file else None
            if verdict.ok and output_file:
                seen = deliverable_seen(result.files, output_file)
                found = None
                if not seen and self.deliverables is not None:
                    session = await repo.get_session(str(task["session_id"]))
                    found = await self.deliverables.exists(
                        owner=str(task["owner_actor_id"]),
                        directory=session.get("project_root"),
                        path=output_file,
                    )
                verdict, written, checked = harden(
                    verdict, written=seen, found=found, output_file=output_file
                )
                await repo.append_event(
                    session_id=str(task["session_id"]),
                    kind="task",
                    event_type="step/deliverable",
                    payload={
                        "step_id": step.step_id,
                        "path": output_file,
                        "seen": written,
                        "checked": checked,
                    },
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
            if verdict.ok:
                art = await repo.put_artifact(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    name=step.output_artifact,
                    kind="step",
                    content=verdict.parsed,
                    workspace_path=output_file if written else None,
                )
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.COMPLETED,
                    consumed=consumed,
                    tokens=result.tokens,
                    output_artifacts=[
                        {
                            "name": art["name"],
                            "version": art["version"],
                            "id": art["id"],
                        }
                    ],
                )
                task = await repo.get_task(task_id)
                await repo.cas_task(
                    task_id,
                    expected_version=int(task["state_version"]),
                    current_step=int(task["current_step"]) + 1,
                )
                await repo.append_event(
                    session_id=str(task["session_id"]),
                    kind="task",
                    event_type="step/accepted",
                    payload={
                        "step_id": step.step_id,
                        "artifact": art,
                        "checks": verdict.checks,
                        "consumed": str(consumed),
                    },
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
                await repo.commit()
                if pack.step(int(task["current_step"]) + 1) is None:
                    return await self._finish(
                        led, repo, await repo.get_task(task_id), pack
                    )
                await self._requeue(led, task_id)
                return TaskState.QUEUED
            # 不合格
            await led.attempt_transition(
                attempt_id,
                AttemptState.FAILED,
                consumed=consumed,
                tokens=result.tokens,
                # 超时也记成验收不过：返工额度照算，不会因为一直超时而无限重做
                failure_code="acceptance",
                retryable=True,
            )
            await repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="step/rejected",
                payload={
                    "step_id": step.step_id,
                    "failures": verdict.failures,
                    "checks": verdict.checks,
                    "rework": reworks,
                    "on_reject": step.on_reject,
                    # 没通过的这一次交回了什么，留着：重做之后还看得到（AT-INV-21）
                    "returned": {
                        "content": verdict.parsed,
                        "raw": None
                        if verdict.parsed is not None
                        else (result.final_text or "")[:RAW_CHARS],
                    },
                },
                task_id=task_id,
                attempt_id=attempt_id,
            )
            await repo.commit()
            missing = missing_data.from_step(step, verdict.parsed)
            if missing is not None:
                return await self._no_data(
                    led, repo, task, step, attempt_id, verdict, missing
                )
            if reworks < step.max_reworks:  # 还有返工额度：重做本步
                await self._requeue(led, task_id)
                return TaskState.QUEUED
            backs = 0
            if step.on_reject == "back" and step.back_to:  # 回到指定的前一步
                backs = sum(
                    1
                    for e in await repo.list_task_events(
                        task_id, types=("step/went_back",)
                    )
                    if e["payload"].get("step_id") == step.step_id
                )
                if backs < step.max_backs:
                    task = await repo.get_task(task_id)
                    await repo.append_event(
                        session_id=str(task["session_id"]),
                        kind="task",
                        event_type="step/went_back",
                        payload={
                            "step_id": step.step_id,
                            "back_to": step.back_to,
                            "time": backs + 1,
                            "of": step.max_backs,
                        },
                        task_id=task_id,
                        attempt_id=attempt_id,
                    )
                    await repo.cas_task(
                        task_id,
                        expected_version=int(task["state_version"]),
                        current_step=pack.index_of(step.back_to),
                    )
                    await repo.commit()
                    await self._requeue(led, task_id)
                    return TaskState.QUEUED
            # 返工用尽、退回的次数也用尽：交人（AT-24）
            it = await led.open_interaction(
                task_id,
                kind="input",
                prompt=InteractionPrompt(
                    title=f"第「{step.title}」步没有通过验收",
                    question=(
                        f"这一步已经退回前面重做过 {backs} 次，回来仍然没有通过。"
                        "要再试一次，还是停止？"
                        if backs
                        else "要再试一次，还是停止？"
                    ),
                    options=[
                        {"id": "rework", "label": "再试一次"},
                        {"id": "stop", "label": "停止"},
                    ],
                    subject={
                        "step_id": step.step_id,
                        "failures": verdict.failures,
                        "last_output": (result.final_text or "")[:2000],
                        "backs": backs,
                    },
                    unknowns=verdict.failures,
                ),
                waiting_reason=WaitingReason.INPUT,
                attempt_id=attempt_id,
            )
            log.info(
                "step %s handed to human interaction=%s",
                step.step_id,
                it["interaction_id"],
            )
            return TaskState.WAITING

    async def _answers_for_step(
        self, repo: WorkbenchStore, task: dict[str, Any], step: StepContract
    ) -> dict[str, Any]:
        """这一步已经问过用户、用户答过的：开步前的问题，和停下 / 返工时附的要求。"""
        out: dict[str, Any] = {
            "ask_before": None,
            "change_request": None,
            "proceed": False,
        }
        consumed = [
            it
            for it in await repo.list_interactions(
                session_id=str(task["session_id"]), status="consumed"
            )
            if str(it.get("task_id") or "") == str(task["id"])
            and ((it.get("prompt") or {}).get("subject") or {}).get("step_id")
            == step.step_id
        ]
        for it in consumed:
            subject = (it.get("prompt") or {}).get("subject") or {}
            response = it.get("response") or {}
            if it["kind"] == "question" and subject.get("ask_before"):
                answers = (
                    response.get("answers")
                    or (response.get("answer") or {}).get("answers")
                    or {}
                )
                out["ask_before"] = {
                    q["id"]: answers.get(q["id"])
                    for q in subject.get("questions") or []
                }
            if it["kind"] == "input":
                answer = response.get("answer")
                note = None
                if isinstance(answer, dict):
                    note = answer.get("text") or answer.get("note")
                elif isinstance(answer, str):
                    note = answer
                note = note or subject.get("note")
                if note and response.get("decision") in ("rework", "proceed"):
                    out["change_request"] = str(note)
                if subject.get("out_of_scope") and response.get("decision") != "stop":
                    out["proceed"] = True
        return out

    async def _no_data(
        self,
        led: Ledger,
        repo: WorkbenchStore,
        task: dict[str, Any],
        step: StepContract,
        attempt_id: str,
        verdict: Any,
        missing: dict[str, Any],
    ) -> str:
        """这家公司没有入库：重做也不会有数据，所以不重做，记一条「没有数据」，直接问用户。"""
        task_id = str(task["id"])
        await repo.append_event_once(
            session_id=str(task["session_id"]),
            kind="data",
            event_type=missing_data.EVENT,
            payload=missing,
            same={"security_code": missing["security_code"]},
            task_id=task_id,
            attempt_id=attempt_id,
        )
        await repo.commit()
        await led.open_interaction(
            task_id,
            kind="input",
            prompt=InteractionPrompt(
                title="这家公司还没有数据",
                question="数据入库之后可以接着做。现在可以先不答：委托停在这里，不再花钱",
                options=[
                    {"id": "rework", "label": "数据已经入库了，接着做"},
                    {"id": "stop", "label": "停止"},
                ],
                subject={
                    "step_id": step.step_id,
                    "failures": verdict.failures,
                    "missing_data": missing,
                },
                unknowns=["这家公司的数据什么时候能入库"],
            ),
            waiting_reason=WaitingReason.INPUT,
            attempt_id=attempt_id,
        )
        return TaskState.WAITING

    async def _out_of_scope(
        self,
        led: Ledger,
        repo: WorkbenchStore,
        task: dict[str, Any],
        pack: ExpertPack,
        step: StepContract,
        attempt_id: str,
        result: TurnResult,
        declined: dict[str, str],
    ) -> str:
        """专家说这个问题不归它：这一步不算做过、不算返工，停下问用户照做还是不做（账 130）。"""
        task_id = str(task["id"])
        await led.attempt_transition(
            attempt_id,
            AttemptState.CANCELLED,
            consumed=result.cost or Decimal("0"),
            tokens=result.tokens,
            failure_code="out_of_scope",
        )
        await repo.append_event(
            session_id=str(task["session_id"]),
            kind="task",
            event_type="step/out_of_scope",
            payload={"step_id": step.step_id, **declined},
            task_id=task_id,
            attempt_id=attempt_id,
        )
        await repo.commit()
        name = pack.name or pack.title
        await led.open_interaction(
            task_id,
            kind="input",
            prompt=InteractionPrompt(
                title=f"这个问题看起来不归「{name}」",
                question="\n".join(
                    line
                    for line in (
                        declined["reason"],
                        f"建议：{declined['suggestion']}"
                        if declined["suggestion"]
                        else "",
                        "要专家照做，还是不做了？",
                    )
                    if line
                ),
                options=[
                    {"id": "proceed", "label": "照做"},
                    {"id": "stop", "label": "不做了"},
                ],
                subject={"step_id": step.step_id, "out_of_scope": True, **declined},
            ),
            waiting_reason=WaitingReason.INPUT,
            attempt_id=attempt_id,
        )
        log.info("task %s declined as out of scope at %s", task_id, step.step_id)
        return TaskState.WAITING

    async def fail_after_crash(self, task_id: str, error: str) -> None:
        """驾驶中抛了没接住的异常：开着的尝试记失败、委托记失败，不让它停在 RUNNING 没人推。

        K19 判分崩了以后委托一直挂着，测试要等到超时才收尾。已经停下（等人、终态）的不动。
        记着取消的收成「已取消」，不算平台失败（0034 §三.1）。
        """
        async with self.stores() as repo:
            led = Ledger(repo)
            task = await repo.get_task(task_id)
            if task["state"] not in (TaskState.QUEUED, TaskState.RUNNING):
                return
            cancelled = task.get("cancel_requested_at") is not None
            attempt_id = task.get("active_attempt_id")
            if attempt_id is not None:
                attempt = await repo.get_attempt(str(attempt_id))
                if attempt["status"] in (AttemptState.CREATED, AttemptState.RUNNING):
                    await led.attempt_transition(
                        str(attempt_id),
                        AttemptState.CANCELLED if cancelled else AttemptState.FAILED,
                        failure_code="cancelled" if cancelled else PLATFORM_FAILURE,
                        retryable=False,
                    )
            if cancelled:
                await led.transition(
                    task_id, TaskState.CANCELLED, reason={"by": "user"}
                )
                return
            await led.transition(task_id, TaskState.FAILED, reason={"error": error})

    async def _requeue(self, led: Ledger, task_id: str) -> bool:
        async with self.stores() as repo:
            task = await repo.get_task(task_id)
            if task["state"] == TaskState.RUNNING:
                await Ledger(repo).transition(task_id, TaskState.QUEUED)
                return True
            return task["state"] == TaskState.QUEUED

    async def _hard_stop(self, led: Ledger, repo: WorkbenchStore, task_id: str) -> str:
        task = await repo.get_task(task_id)
        # 没有 Attempt 可开：直接进 WAITING(RESOURCE)，让用户追加
        await led.open_interaction(
            task_id,
            kind="resource",
            prompt=InteractionPrompt(
                title="预算不足以开始下一步",
                question="是否追加预算继续？",
                options=[
                    {"id": "topup", "label": "追加"},
                    {"id": "stop", "label": "停止"},
                ],
                subject={"budget": task["budget"]},
            ),
            waiting_reason=WaitingReason.RESOURCE,
        )
        return TaskState.WAITING

    async def _finish(
        self,
        led: Ledger,
        repo: WorkbenchStore,
        task: dict[str, Any],
        pack: ExpertPack,
    ) -> str:
        task_id = str(task["id"])
        last = pack.workflow[-1]
        result_art = await repo.get_artifact(task_id=task_id, name=last.output_artifact)
        attempts = await repo.list_attempts(task_id)
        did = [f"{a['step_id']}: {a['status']}" for a in attempts]
        verified = [e for e in await repo.list_artifacts(task_id)]
        files = sorted(
            {a["workspace_path"] for a in verified if a.get("workspace_path")}
        )
        handback = {
            "did": did,
            "verified": [
                {"artifact": a["name"], "version": a["version"], "digest": a["digest"]}
                for a in verified
            ],
            "unknown": [],
            # 成稿在项目目录里（0015）；这里只记路径作证据
            "files": files,
            "workspace_restore": (
                "成稿写在项目目录：" + "、".join(files) + "；不要就删掉这些文件"
                if files
                else "顾问没有登记写出的成稿文件；如有改动见各步 Artifact 与对话里的改动面板"
            ),
            "budget": task["budget"],
        }
        hb = await repo.put_artifact(
            task_id=task_id,
            attempt_id=None,
            name="handback",
            kind="handback",
            content=handback,
        )
        await repo.commit()
        task = await repo.get_task(task_id)
        if task["state"] == TaskState.QUEUED:  # 重启后从游标续到末尾的情形
            await led.transition(task_id, TaskState.RUNNING)
        r = await led.transition(
            task_id,
            TaskState.SUCCEEDED,
            terminal_result_ref=result_art["id"] if result_art else hb["id"],
        )
        return r["state"]

    # ---------------- turn 输入 ----------------
    @staticmethod
    def compose(
        step: StepContract,
        question: dict[str, Any],
        inputs: dict[str, Any],
        reworks: int,
        *,
        rework_failures: list[str] | None = None,
        records_task: str | None = None,
        data_task: str | None = None,
        ask_before_answers: dict[str, Any] | None = None,
        change_request: str | None = None,
        user_facts: str | None = None,
        last_step: bool = False,
        folder: str | None = None,
        intake: Scope | None = None,
    ) -> str:
        parts = [
            f"# Advisor step: {step.title} ({step.step_id}, v{step.step_version})",
            "You are executing one step of a supervised research workflow. Do exactly this step, nothing beyond it.",
            "",
            "## The user's question",
            json.dumps(question, ensure_ascii=False),
            "",
        ]
        if intake:
            parts += [
                "## First: is this question for this expert?",
                f"This expert does: {intake.does}",
                *([f"It does not do: {intake.does_not}"] if intake.does_not else []),
                *([f"Elsewhere: {intake.elsewhere}"] if intake.elsewhere else []),
                "If the question is clearly unrelated to what this expert does (small "
                "talk, general knowledge, coding, or a different kind of task), do not do "
                "this step and call no tools: reply with ONE JSON object only, "
                '{"out_of_scope": true, "reason": "<one sentence>", "suggestion": '
                '"<where or how to ask instead>"}, written in the language of the '
                "question. A vague, broad or partly covered question is NOT out of "
                "scope: do the step and ask or state assumptions as the method says.",
                "",
            ]
        parts += [
            "## Method for this step",
            step.method_text,
            "",
            "## Rules",
            NO_DETOUR,
            "If you need information you cannot obtain, ask the user with the request_user_input tool "
            "when it is available; if you get no answer, state your assumption explicitly and continue.",
        ]
        if user_facts:
            parts += ["", "## About the user", user_facts]
        if ask_before_answers:
            parts += ["", "## Answers the user gave before this step"]
            for q in step.ask_before:
                parts.append(
                    f"- {q.question}: {ask_before_answers.get(q.id) or '(no answer)'}"
                )
        if change_request:
            parts += [
                "",
                "## The user's change request for this step",
                change_request,
                "Redo this step following the request above.",
            ]
        if step.tools:
            parts += ["", "## Tools allowed in this step", ", ".join(step.tools)]
        if inputs:
            parts += ["", "## Inputs (fixed artifact versions from earlier steps)"]
            for name, v in inputs.items():
                body = json.dumps(v["content"], ensure_ascii=False)
                parts.append(f"### {name} (v{v['version']})")
                parts.append(body[: step.input_max_chars])
                if len(body) > step.input_max_chars:
                    # 截断要说出来：不说的话，模型会把残缺的输入当成完整的
                    parts.append(
                        f"[TRUNCATED: `{name}` is {len(body)} characters, only the "
                        f"first {step.input_max_chars} are shown. Say so in your "
                        "output and do not assume what the missing part contains.]"
                    )
        if records_task:
            parts += [
                "",
                "## Records of this project (read only when needed)",
                "Earlier conversations of this project can be read with the tools "
                "list_project_conversations and read_project_conversation. Pass "
                f'task="{records_task}" on every call. Earlier deliverables are files '
                "in the project directory: read them there. Use these only when the "
                "question refers to earlier work in this project. They never replace "
                "the data tools: numbers still come from the data tools of this step.",
            ]
        if data_task:
            # 分身读用户自己绑定的数据（0027 §3.5）：步骤的工具里列了数据工具才说
            parts += [
                "",
                "## The user's own data",
                "The user bound their own data to this expert. List it with "
                "list_my_data and read it with read_my_data; pass "
                f'task="{data_task}" on every call. Numbers come only from this data '
                "and the inputs above: quote the citation of each number you use. If "
                "the data does not contain what this step needs, say so instead of "
                "guessing.",
            ]
            if {"read_my_table", "compute"} & set(step.tools):
                # 0030：算出来的数由平台算，取出来的数写地址；数值验收按编号与地址回查
                parts += [
                    "Read tables with read_my_table to see the cell addresses. Compute "
                    "every derived number with compute, never in your head; put its id "
                    "as compute_id next to the number. For a number taken from the data "
                    "as is, write its cell address next to it. Do not copy numbers by "
                    "hand.",
                ]
        if step.output_file:
            # 成稿是项目里的文件，给用户看的唯一一份（0015 §3.3）；JSON 仍回正文做机器验收
            parts += [
                "",
                "## Deliverable file",
                f"Write the deliverable of this step to `{placed(step.output_file, folder)}` (path relative "
                "to the project directory; create the folders if needed; the folder is "
                "for this request only, so files of earlier requests stay as they are). "
                + WRITE_FILE_RULES
                + " "
                + DELIVERABLE_RULES,
            ]
            if last_step:
                handback = f"{folder}/{HANDBACK_FILE}" if folder else HANDBACK_FILE
                parts += ["", "## Handback file", handback_rules(handback)]
        parts += [
            "",
            "## Output",
            f"Produce the artifact `{step.output_artifact}` as ONE JSON object with this shape:",
            json.dumps(step.output_schema, ensure_ascii=False),
        ]
        if reworks:
            parts += [
                "",
                f"## Note: this is rework #{reworks}; the previous attempt failed acceptance. Follow the output shape exactly.",
            ]
            if rework_failures:
                parts += ["What failed last time (fix these):"] + [
                    f"- {one_line(str(f), REWORK_FAILURE_CHARS)}"
                    for f in rework_failures[:REWORK_FAILURES_SHOWN]
                ]
        return "\n".join(parts)
