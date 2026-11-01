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

from app.application.ports.workbench import WorkbenchStore, WorkbenchStores
from app.application.workbench.acceptance import judge, out_of_scope, timed_out
from app.application.workbench.acceptance_numbers import judge_numbers
from app.application.workbench.catalog import SNAPSHOT_SOURCES, pack_for_task
from app.application.workbench.deliverable_check import DeliverableCheck, harden
from app.application.workbench.expert_data import holds_task, pending_requests
from app.application.workbench.ledger import Ledger
from app.domain.workbench import knowbots
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
from app.domain.workbench.packs import (
    SEALED_INPUT,
    ExpertPack,
    Scope,
    StepContract,
    find_pack,
)
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
# 第 0 步「定数据」（decisions ⑭）：不在专家的步骤里，不验收、不进游标
DATA_STEP_ID = "data-plan"
DATA_STEP_TITLE = "定数据"
DATA_STEP_RESERVE = Decimal("0.05")
# 给模型看的专家做法：每一步的说明最多这么多字，合起来最多这么多字
DATA_STEP_METHOD_CHARS = 1200
DATA_STEP_TOTAL_CHARS = 12000


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
        reader: Any = None,
    ):
        self.stores = stores  # 每次调用开一个工作单元
        self.driver = driver
        # 沙箱里配好了读项目记录的工具：每一步的说明里告诉模型有这组工具、委托编号是什么
        self.records = records
        # 成稿硬检查（0022 段 4）：没配就只有软检查
        self.deliverables = deliverables
        # 数值验收 sourced 回查事实编号（0030 §3.3）、quoted 回资料那一页核引文（0036），
        # 都凭委托的运行授权读 knowbase（run_reader.RunReader）：没配就判不过，不放行
        self.reader = reader

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
                held = await self._data_hold(repo, led, task_id)
                if held is not None:
                    return held
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

    async def _data_hold(self, repo, led: Ledger, task_id: str) -> str | None:
        """清单 v2 的专家：要补采的数据还没齐就进「等数据」（齐了 DataWaiter 送回来）；
        没取到就打回并说清缺什么。锁住清单那一行，和 DataWaiter 不会两边都错过。"""
        async with repo.transaction():
            row = await repo.get_task_resolution(task_id, for_update=True)
            if row is None or row["status"] == "planning":
                return None  # 定数据在第 0 步（_plan_data）
            if row["status"] == "ready" and not holds_task(row):
                return None
            if holds_task(row):
                await led.transition(
                    task_id,
                    TaskState.WAITING,
                    waiting_reason=WaitingReason.DATA,
                    reason=_waiting_reason(row),
                )
                return TaskState.WAITING
            await led.transition(
                task_id,
                TaskState.REJECTED,
                reason={
                    "code": "data_unavailable",
                    "message": str(
                        (row.get("detail") or {}).get("reason") or "要的数据没取到"
                    ),
                },
            )
            return TaskState.REJECTED

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
            row = await repo.get_task_resolution(task_id)
            planning = row is not None and row["status"] == "planning"
        if planning:
            return await self._plan_data(task_id)
        async with self.stores() as repo:
            led = Ledger(repo)
            task = await repo.get_task(task_id)
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
            # 孵化方法（专家孵化方案第八节）：模型只看契约列死的材料（0029 第 6.4 节），
            # 不加用户的记忆、不给读项目记录的说明
            sealed = pack.profile_id in SEALED_INPUT
            text = self.compose(
                step,
                question,
                inputs,
                reworks,
                rework_failures=rejected[-1].get("failures") if rejected else None,
                folder=folder,
                records_task=task_id if self.records and not sealed else None,
                tool_task=task_id if self.records else None,
                listed_data_task=(
                    task_id
                    if self.records
                    and task.get("pack_source") in SNAPSHOT_SOURCES
                    and knowbots.DATA_MCP_TOOLS & set(step.tools)
                    else None
                ),
                ask_before_answers=answered["ask_before"],
                change_request=answered["change_request"],
                user_facts=None
                if sealed
                else memory_note(
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
                    reader=self.reader,
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
                # 专家这一步要了新数据（request_data）：在步骤边界停下等用户确认、补齐
                async with repo.transaction():
                    row = await repo.get_task_resolution(task_id, for_update=True)
                    if holds_task(row):
                        await led.transition(
                            task_id,
                            TaskState.WAITING,
                            waiting_reason=WaitingReason.DATA,
                            reason=_waiting_reason(row),
                        )
                        return TaskState.WAITING
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

    async def _plan_data(self, task_id: str) -> str:
        """第 0 步「定数据」（decisions ⑭）：模型读专家的做法和用户的问题，用「查主体」
        「提出数据」两个工具提这件委托要哪些数据。不验收、不进游标；提了就停在步骤之间
        等用户确认（确认时能换、改、去掉、加），没提就接着第 1 步。"""
        async with self.stores() as repo:
            led = Ledger(repo)
            task = await repo.get_task(task_id)
            pack = pack_for_task(task)
            assert pack is not None
            if not self.records:
                # 沙箱里没配工作台的工具：提不了，照现有的做
                async with repo.transaction():
                    await repo.update_task_resolution(task_id, status="ready")
                await repo.commit()
                return TaskState.QUEUED
            try:
                opened = await led.open_attempt(
                    task_id,
                    step_id=DATA_STEP_ID,
                    step_version="1",
                    input_artifact_versions=[],
                    reserve=DATA_STEP_RESERVE,
                    meta={"data_step": True},
                )
            except BudgetExhausted as exc:
                log.info("budget exhausted before the data step: %s", exc)
                return await self._hard_stop(led, repo, task_id)
            attempt_id = opened["attempt_id"]
            await repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="step/started",
                payload={
                    "step_id": DATA_STEP_ID,
                    "title": DATA_STEP_TITLE,
                    "attempt_id": attempt_id,
                    "rework": 0,
                },
                task_id=task_id,
                attempt_id=attempt_id,
            )
            await repo.commit()
            bound = sum(
                1
                for p in await repo.list_task_data(task_id)
                if p.get("basis") == "frozen_binding"
            )
            text = compose_data_plan(
                pack, task["original_input"].get("input", {}), task_id, bound
            )
            thread_id = task["thread_id"]

        result = await self.driver.run_turn(
            thread_id, text, timeout=STEP_TIMEOUT_SECONDS
        )

        async with self.stores() as repo:
            led = Ledger(repo)
            consumed = result.cost or Decimal("0")
            await led.end_turn_waits(task_id)
            settled = (await repo.get_attempt(attempt_id))["status"] in ATTEMPT_TERMINAL
            if not settled:
                try:
                    await led.attempt_transition(
                        attempt_id, AttemptState.RUNNING, turn_id=result.turn_id
                    )
                except InvalidTransition:
                    settled = (await repo.get_attempt(attempt_id))[
                        "status"
                    ] in ATTEMPT_TERMINAL
                    if not settled:
                        raise
            if settled:
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
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.CANCELLED,
                    consumed=consumed,
                    tokens=result.tokens,
                    failure_code="cancelled",
                )
                if task["state"] in TASK_TERMINAL:
                    return task["state"]
                return (
                    await led.transition(
                        task_id, TaskState.CANCELLED, reason={"by": "user"}
                    )
                )["state"]
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
                if result.environment_lost:
                    return (
                        await led.transition(
                            task_id,
                            TaskState.WAITING,
                            waiting_reason=WaitingReason.ENVIRONMENT,
                            reason={"error": result.error},
                        )
                    )["state"]
                return (
                    await led.transition(
                        task_id, TaskState.FAILED, reason={"error": result.error}
                    )
                )["state"]
            limit = task["budget"].get("limit")
            if limit is not None and consumed > Decimal(str(limit)) - Decimal(
                str(task["budget"]["used"])
            ):
                await led.attempt_transition(
                    attempt_id,
                    AttemptState.BUDGET_EXCEEDED,
                    consumed=consumed,
                    tokens=result.tokens,
                )
                return TaskState.WAITING
            # 用户在审批面停下也算定完了：提了几样就确认几样，没提就照现有的做
            await led.attempt_transition(
                attempt_id,
                AttemptState.COMPLETED,
                consumed=consumed,
                tokens=result.tokens,
                output_artifacts=[],
            )
            row = await repo.get_task_resolution(task_id)
            asked = len(pending_requests(row))
            await repo.append_event(
                session_id=str(task["session_id"]),
                kind="task",
                event_type="step/accepted",
                payload={
                    "step_id": DATA_STEP_ID,
                    "requests": asked,
                    "consumed": str(consumed),
                },
                task_id=task_id,
                attempt_id=attempt_id,
            )
            async with repo.transaction():
                await repo.update_task_resolution(task_id, status="ready")
                row = await repo.get_task_resolution(task_id, for_update=True)
                if holds_task(row):
                    await led.transition(
                        task_id,
                        TaskState.WAITING,
                        waiting_reason=WaitingReason.DATA,
                        reason=_waiting_reason(row),
                    )
                    return TaskState.WAITING
            await self._requeue(led, task_id)
            return TaskState.QUEUED

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
            # 平台的事（推它的程序崩了）：测试结果里记出错，不记没过
            await led.transition(
                task_id, TaskState.FAILED, reason={"error": error, "platform": True}
            )

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
        if files and task.get("pack_source") != "knowlab-test":
            # 成稿替用户写进 knowbase 产出区（SDD 0043 第 2.6 节）：runner 里另一条线写，
            # 写不进去不挡交付；测试的证据由 knowlab 写
            # （委托成功是终态，一件委托只交付一次，运行编号就用委托编号：续跑重进这里也不重记）
            await repo.add_task_outputs(
                task_id,
                run_ref=task_id,
                files=await self._fingerprints(repo, task, files),
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

    async def _fingerprints(
        self, repo: WorkbenchStore, task: dict[str, Any], files: list[str]
    ) -> dict[str, str | None]:
        """交付那一刻给全部成稿各取一次 sha256（本地 link 的 fs/digest，在用户电脑上算）：
        写进 knowbase 之前按它核是不是交付的那份（0043 第 2.6 节「出处照实」）。取不到的
        记 None，写的时候改看 link 报的修改时间（不晚于交付就认）；不挡交付。"""
        digest = getattr(self.deliverables, "digest", None)
        if digest is None or not files:
            return dict.fromkeys(files)
        session = await repo.get_session(str(task["session_id"]))
        out: dict[str, str | None] = {}
        for path in files:
            try:
                out[path] = await digest(
                    owner=str(task["owner_actor_id"]),
                    directory=session.get("project_root"),
                    path=path,
                )
            except Exception:  # noqa: BLE001 — 取不到不挡交付
                out[path] = None
        return out

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
        tool_task: str | None = None,
        listed_data_task: str | None = None,
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
        if tool_task and "find_expert" in step.tools:
            # 找专家（请专家方案设计第 4.9 节）：目录经工作台查 knowmart，凭这件委托
            parts += [
                "",
                "## The expert directory",
                f'Call find_expert(task="{tool_task}") to list the experts this user can '
                "ask. It returns only what each expert published about itself.",
            ]
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
        if listed_data_task:
            # 清单 v2 的专家（0048）：数据经工作台中继到 knowbase 数据 MCP，钉住的就是这件委托能读的
            parts += [
                "",
                "## The data of this task",
                "The data for this task was confirmed by the user before step 1. List it "
                f'with list_inputs and read it with read_input; pass task="{listed_data_task}" '
                "on every data call. Take figures with query_facts (each row has a ref and "
                "cites material, version and page) or query_metric; find passages with "
                "search_evidence. Numbers come only from this data and the inputs above: "
                "never from memory. If the data does not contain what this step needs, "
                "ask for it with request_data and say in this step what is missing; do not "
                "guess.",
            ]
            if "compute" in step.tools:
                parts += [
                    "Compute every derived number with compute, never in your head: give "
                    "fact refs (fact:… from query_facts) as its inputs and put the returned "
                    "id as compute_id next to the number. For a figure taken as is, write "
                    "its fact ref as the address. Quote passages as {material_id, page, "
                    "text} exactly as read.",
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


def _waiting_reason(row: dict[str, Any]) -> dict[str, Any]:
    """「等数据」给页面看的：在补采哪几项，或专家新要了什么、等确认。"""
    detail = row.get("detail") or {}
    pending = [r for r in pending_requests(row) if r.get("status") == "pending"]
    if pending:
        return {
            "data": "requested",
            "requests": [r["id"] for r in pending],
            "waiting_for": [label for r in pending for label in r.get("labels") or []],
        }
    missing = [r for r in pending_requests(row) if r.get("status") == "missing"]
    if missing:
        return {
            "data": "missing",
            "missing": [item for r in missing for item in r.get("missing") or []],
        }
    return {"data": "waiting", "waiting_for": detail.get("waiting_for")}


def compose_data_plan(pack: ExpertPack, question: Any, task_id: str, bound: int) -> str:
    """第 0 步「定数据」给模型的说明（decisions ⑭）：专家的做法、用户的问题、专家自带
    几份；用查主体、提出数据两个工具提要哪些，不开始做第 1 步。"""
    method: list[str] = []
    room = DATA_STEP_TOTAL_CHARS
    for n, step in enumerate(pack.workflow, start=1):
        body = step.method_text.strip()[:DATA_STEP_METHOD_CHARS]
        chunk = f"{n}. {step.title}\n{body}"
        if len(chunk) > room:
            method.append(f"{n}. {step.title}")
            continue
        room -= len(chunk)
        method.append(chunk)
    asked = question.get("text") if isinstance(question, dict) else str(question or "")
    parts = [
        f"# Step 0 of {pack.name or pack.title}: decide what data this task needs",
        "Before step 1 starts, decide which data the steps below need for this "
        "user's question, and propose it. Do not start step 1 in this turn.",
        "",
        "## The user's question",
        str(asked or "").strip()[:4000],
        "",
        "## How this expert works (its steps)",
        *method,
        "",
        "## What to do",
        "- Work out which companies or institutions the question is about. Look up "
        f'each code with find_subject(task="{task_id}", name=…); never write a code '
        "from memory. Ask for data on other companies too when the steps compare "
        "with them.",
        f'- Propose the data with request_data(task="{task_id}", needs=[…]): for each '
        "item give subject (the code; leave it out for data about no particular "
        "company, such as rules or statistics), doc_type, period, reading, "
        "essential (true if the steps cannot be done without it) and reason (why "
        "the steps need it). At most 20 items.",
        "- The user confirms, changes or drops these items before anything is "
        "fetched or read; you cannot read them in this turn.",
        "- If the question needs no data, propose nothing.",
        "- End the turn with one sentence on what you proposed and why.",
    ]
    if bound:
        parts += [
            "",
            f"The expert also brings {bound} item(s) of its own, chosen by its author; "
            "they are already there for the steps. Do not ask for them.",
        ]
    return "\n".join(parts)
