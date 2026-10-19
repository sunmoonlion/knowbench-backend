"""作为运行方跑分身测试（SDD 0027 §3.4，契约 knowbot-test-run/v1，有人值守）。

knowlab 交来一份草稿清单与验收用例。我们用真引擎跑：测试账号、集群里的测试电脑、测试账号
自己的沙箱与模型 key。每条用例按顺序在测试电脑的固定项目里开一段新的专家对话，用草稿的
专家包快照建委托（来源 knowlab-test，不进钉表、不进用户的专家清单），runner 照常驱动。

跑到要人决定的地方（工作台的「待人回答」交互）就报成待回答的问题：开步前的提问先用用例里
预给的答案；审批与 Codex 反问报给 knowlab，那边运营答跑法、用户答投资判断，答完我们代测试
账号回答，委托接着跑。验收不过、返工用完要交人的那种不问，这条用例记 failed。等人的时间不计
入超时，一个问题最多等 max_wait_seconds，过了这条用例记 needs_human。中途的审批与反问还受
runner 的一小时等待上限约束（Codex 那一轮挂在沙箱里），超时按停下收尾，这条用例记 needs_human。

结果只有运行正确与否：每步验收、没过的验收名、期望满足与否，不放分析内容。每条用例跑完时另取
它的产出——成稿与各步交回物（SDD 0037）：这时取，是因为用例共用工作区、后一条会盖掉前一条的
成稿，整次测完目录还要删。产出单独存、单独一个接口给 knowlab（运营验证这一稿、用户看自己的）。
测试产出 7 天后删除（委托、对话、交回物；测试电脑上的底稿由测试电脑自己清理）；取好的产出
30 天后清空，测试结果留着。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.application.ports.workbench import WorkbenchStore, WorkbenchStores
from app.application.workbench.catalog import SOURCE_TEST, PinnedPack
from app.application.workbench.deliverable_check import (
    DeliverableReader,
    DeliverableTooLarge,
)
from app.application.workbench.expert_desk import ExpertDesk
from app.application.workbench.interactions import answer_interaction, cancel_task
from app.application.workbench.ledger import Ledger
from app.application.workbench.project_service import ONLINE
from app.application.workbench.session_service import SessionService
from app.application.workbench.test_folders import (
    FolderRefused,
    TestFolders,
    run_folder,
)
from app.domain.workbench import knowbots
from app.domain.workbench.errors import (
    InteractionRejected,
    KnowbotTestBudgetExhausted,
    KnowbotTestConflict,
    KnowbotTestUnconfigured,
    NotFound,
    QuestionNotOpen,
    WorkbenchError,
)
from app.domain.workbench.models import HandoverRequest
from app.domain.workbench.states import TASK_TERMINAL, TaskState
from app.domain.workbench.test_runs import (
    CONTRACT,
    FINAL,
    Answers,
    RunCase,
    RunRequest,
    case_outputs,
    case_result,
    trial_profile_id,
)
from app.domain.workbench.tool_policy import DECISION_ACCEPT, DECISION_DECLINE

log = logging.getLogger(__name__)

ACTIVE = ("accepted", "running", "waiting")
KEEP = timedelta(days=7)  # 测试产出保留 7 天（0027 决定 11 ④）
KEEP_OUTPUTS = timedelta(
    days=30
)  # 取好的成稿与交回物留 30 天，knowlab 早取走了（SDD 0037）
NO_KNOWBOT = "00000000-0000-0000-0000-000000000000"  # 草稿还没有分身 id


@dataclass(frozen=True)
class KnowbotTestConfig:
    """测试账号（Casdoor 用户 id）、测试电脑上的工作区根目录、每月测试费用上限。"""

    subject: str | None
    workspace_root: str | None
    monthly_limit: Decimal = Decimal("1000")
    # 同时在跑的测试最多几个，其余按受理先后排队（账 129）：只有一台测试电脑、一个沙箱、一把 key，
    # 一起跑会互相拖慢、超时，也会在同一个工作区里看到彼此的产出
    max_running: int = 1


@dataclass(frozen=True)
class Account:
    actor_id: str
    environment_id: str
    project_id: str
    online: bool = True  # 测试电脑在线
    # 测试账号下此刻在线的别的电脑（账 133）：一个账号同一时刻只有一台在线，有它测试电脑就上不来
    others_online: tuple[str, ...] = ()


def queued_behind(task: dict[str, Any]) -> str | None:
    """委托还没开工、排在同一项目里哪件委托后面；没在排队就是 None。"""
    if task.get("state") != TaskState.RECEIVED:
        return None
    context = (task.get("original_input") or {}).get("client_context") or {}
    behind = context.get("queued_behind")
    return str(behind) if behind else None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _request(run: dict[str, Any]) -> RunRequest:
    return RunRequest.model_validate(run["request"])


def _questions_of(
    interaction: dict[str, Any], answered: set[str]
) -> list[dict[str, Any]]:
    """一个「待人回答」交互 → 契约里的待回答问题。已经答过的子问题不再列。"""
    iid = str(interaction["id"])
    prompt = interaction.get("prompt") or {}
    subject = prompt.get("subject") or {}
    asked = _iso(interaction.get("created_at")) or ""
    step_id = subject.get("step_id") or (subject.get("step") or {}).get("step_id")
    if interaction["kind"] == "tool_approval":
        if iid in answered:
            return []
        what = (
            subject.get("command")
            or subject.get("path")
            or prompt.get("question")
            or prompt.get("title")
        )
        return [
            {
                "question_id": iid,
                "step_id": step_id,
                "kind": "tool_approval",
                "key": None,
                "text": f"{prompt.get('title') or '需要批准'}：{what}"[:4000],
                "options": [],
                "allow_free_text": False,
                "asked_at": asked,
            }
        ]
    if interaction["kind"] != "question":
        return []
    ask_before = bool(subject.get("ask_before"))
    out = []
    for q in subject.get("questions") or []:
        qid = f"{iid}:{q.get('id')}"
        if qid in answered:
            continue
        options = [
            {
                "id": str(o.get("id") or o.get("label") or "")[:64],
                "label": str(o.get("label") or o.get("id") or "")[:200],
            }
            for o in q.get("options") or []
            if (o.get("id") or o.get("label"))
        ]
        out.append(
            {
                "question_id": qid[:128],
                "step_id": step_id,
                "kind": "ask_before" if ask_before else "question",
                "key": str(q.get("id")) if ask_before else None,
                "text": str(q.get("question") or prompt.get("question") or "")[:4000],
                "options": options[:20],
                "allow_free_text": bool(q.get("is_other")) or not options,
                "asked_at": asked,
            }
        )
    return out


class KnowbotTestService:
    """契约的三个接口：受理、查状态、回答。真正往前推的是 KnowbotTestConductor。"""

    def __init__(
        self,
        repo: WorkbenchStore,
        config: KnowbotTestConfig,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.config = config
        self.now = now

    async def accept(self, request: RunRequest, *, caller: str) -> dict[str, Any]:
        if not self.config.subject or not self.config.workspace_root:
            raise KnowbotTestUnconfigured("the test account or computer is not set up")
        prior = await self.repo.get_test_run(request.run_id)
        if prior is not None:
            if prior["digest"] != request.digest():
                raise KnowbotTestConflict(
                    "this run id was submitted with other content",
                    run_id=request.run_id,
                )
            return await self.status(request.run_id)
        manifest = request.manifest
        knowbots.pack_from_manifest(  # 不合格抛 KnowbotInvalid（422），不受理
            manifest,
            knowbot_id=NO_KNOWBOT,
            version=str(manifest.get("version")),
            profile=trial_profile_id(request.run_id),
        )
        limit = Decimal(request.budget.limit)
        async with self.repo.transaction():
            committed = await self.repo.committed_test_spend(_month_start(self.now()))
            if committed + limit > self.config.monthly_limit:
                raise KnowbotTestBudgetExhausted(
                    "the monthly test budget would be exceeded",
                    committed=str(committed),
                    limit=str(self.config.monthly_limit),
                )
            await self.repo.insert_test_run(
                {
                    "run_id": request.run_id,
                    "caller": caller,
                    "owner_subject": request.owner_subject,
                    "digest": request.digest(),
                    "request": request.model_dump(mode="json"),
                    "budget_limit": limit,
                    "timeout_seconds": request.timeout_seconds,
                    "max_wait_seconds": request.max_wait_seconds,
                },
                [
                    {"case_id": c.case_id, "position": n}
                    for n, c in enumerate(request.cases)
                ],
            )
        return await self.status(request.run_id)

    async def status(self, run_id: str) -> dict[str, Any]:
        run = await self.repo.get_test_run(run_id)
        if run is None:
            raise NotFound("no such test run", run_id=run_id)
        cases = await self.repo.list_test_cases(run_id)
        questions: list[dict[str, Any]] = []
        if run["status"] not in FINAL:
            questions = await self.open_questions(run, cases)
        status = run["status"]
        if status not in FINAL:
            status = (
                "waiting"
                if questions
                else ("accepted" if run["started_at"] is None else "running")
            )
        return {
            "contract": CONTRACT,
            "run_id": run_id,
            "status": status,
            "cases": [c["result"] for c in cases if c.get("result")],
            "cost": {"spent": str(Decimal(run["spent"]).quantize(Decimal("0.000001")))},
            "started_at": _iso(run.get("started_at")),
            "finished_at": _iso(run.get("finished_at")),
            "questions": questions,
            "waited_seconds": int(run.get("waited_seconds") or 0),
            **(
                {"progress": progress}
                if run["status"] not in FINAL
                and (progress := await self.progress(run, cases)) is not None
                else {}
            ),
        }

    async def progress(
        self, run: dict[str, Any], cases: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """跑着的那条用例走到哪了（待办账 135）：第几条、第几步、步名、这一步第几次、到现在花了
        多少（跑完的几条加上跑着的这条）。只有编号、计数和钱，不含交回物内容（C-L5）。"""
        current = next(
            (c for c in cases if not c.get("result") and c.get("task_id")), None
        )
        if current is None:
            return None
        task = await self.repo.get_task(str(current["task_id"]))
        view = await ExpertDesk(self.repo).steps(
            str(task["id"]), owner_actor_id=str(task["owner_actor_id"])
        )
        at = view["position"] or {"step": 0, "of": 0, "title": ""}
        steps = view["steps"]
        attempt = (
            int(steps[at["step"] - 1]["times"]) if 0 < at["step"] <= len(steps) else 0
        )
        used = Decimal(str((task.get("budget") or {}).get("used") or "0"))
        spent = (Decimal(run["spent"]) + used).quantize(Decimal("0.000001"))
        behind = queued_behind(task)
        return {
            "case_id": current["case_id"],
            "cases_done": sum(1 for c in cases if c.get("result")),
            "cases_total": len(cases),
            "step": at["step"],
            "steps_total": at["of"],
            "step_title": str(at["title"])[:200],
            "attempt": min(attempt, 100),
            "spent": str(spent),
            # 排在别的委托后面：只给那件委托的编号（C-L5），管理端写「排在…后面」（0034 §三.3）
            **({"queued_behind": behind} if behind else {}),
        }

    async def open_questions(
        self, run: dict[str, Any], cases: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """正在跑的那条用例里等人回答的问题。预给了答案的开步前提问不算（推进器会代答）。"""
        request = _request(run)
        by_id = {c.case_id: c for c in request.cases}
        out: list[dict[str, Any]] = []
        for case in cases:
            if case.get("result") or not case.get("task_id"):
                continue
            task = await self.repo.get_task(str(case["task_id"]))
            if task["state"] != TaskState.WAITING or not task.get(
                "active_interaction_id"
            ):
                continue
            interaction = await self.repo.get_interaction(
                str(task["active_interaction_id"])
            )
            answered = await self._answered_ids(run["run_id"], interaction)
            preset = by_id[case["case_id"]].input.answers
            for q in _questions_of(interaction, answered):
                if q["kind"] == "ask_before" and q["key"] in preset:
                    continue
                out.append({**q, "case_id": case["case_id"]})
        return out

    async def _answered_ids(self, run_id: str, interaction: dict[str, Any]) -> set[str]:
        iid = str(interaction["id"])
        ids = [iid] + [
            f"{iid}:{q.get('id')}"
            for q in ((interaction.get("prompt") or {}).get("subject") or {}).get(
                "questions"
            )
            or []
        ]
        return {i for i in ids if await self.repo.get_test_answer(run_id, i)}

    async def outputs(self, run_id: str, case_id: str) -> dict[str, Any]:
        """一条用例跑完时取好的产出（SDD 0037）。还没跑完、这次没取、已清空的都是 404。"""
        if await self.repo.get_test_run(run_id) is None:
            raise NotFound("no such test run", run_id=run_id)
        found = await self.repo.get_test_case_outputs(run_id, case_id)
        if not found:
            raise NotFound("no outputs for this case", run_id=run_id, case_id=case_id)
        return dict(found)

    async def answer(self, run_id: str, answers: Answers) -> dict[str, Any]:
        run = await self.repo.get_test_run(run_id)
        if run is None:
            raise NotFound("no such test run", run_id=run_id)
        cases = await self.repo.list_test_cases(run_id)
        open_ids = (
            {q["question_id"] for q in await self.open_questions(run, cases)}
            if run["status"] not in FINAL
            else set()
        )
        async with self.repo.transaction():
            for given in answers.answers:
                body = given.model_dump(
                    mode="json",
                    exclude_none=True,
                    exclude={"question_id", "answered_by"},
                )
                prior = await self.repo.get_test_answer(run_id, given.question_id)
                if prior is not None:
                    if prior["answer"] != body:
                        raise KnowbotTestConflict(
                            "this question was answered differently",
                            question_id=given.question_id,
                        )
                    continue
                if given.question_id not in open_ids:
                    raise QuestionNotOpen(
                        "this question is not waiting for an answer",
                        question_id=given.question_id,
                    )
                await self.repo.insert_test_answer(
                    run_id, given.question_id, body, given.answered_by
                )
        return await self.status(run_id)


class KnowbotTestConductor:
    """把在跑的测试往前推一步：开下一条用例、代答、管等待与超时、收结果、到期清理。"""

    def __init__(
        self,
        stores: WorkbenchStores,
        config: KnowbotTestConfig,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        folders: TestFolders | None = None,
        reader: DeliverableReader | None = None,
    ) -> None:
        self.stores = stores
        self.config = config
        self.now = now
        # 每次测试自己的目录，开跑前建好、测完删掉（账 129）；没配会合点服务流时为 None，只分项目不建目录
        self.folders = folders
        # 经会合点读测试电脑上的成稿（SDD 0037）；没配会合点时为 None，产出里写明没读
        self.reader = reader
        self._unanswerable: set[str] = set()  # 答不进去、已记过原因的交互
        self._outputs_cleared_at: datetime | None = None  # 上一次清过期产出

    async def tick(self) -> int:
        async with self.stores() as repo:
            runs = await repo.list_test_runs(statuses=ACTIVE)
        # 已经开跑的接着推；空出来的位子按受理先后给排队的
        started = [r for r in runs if r["started_at"] is not None]
        free = max(0, self.config.max_running - len(started))
        runs = started + [r for r in runs if r["started_at"] is None][:free]
        moved = 0
        for run in runs:
            try:
                await self.advance(run["run_id"])
                moved += 1
            except Exception:  # noqa: BLE001 - 一次测试出错不拖累别的
                log.exception("test run advance failed run=%s", run["run_id"])
        await self.cleanup()
        return moved

    async def _account(self, repo: WorkbenchStore, folder: str) -> Account:
        if not self.config.subject or not self.config.workspace_root:
            raise KnowbotTestUnconfigured("the test account or computer is not set up")
        actor = await repo.actor_for_subject(self.config.subject)
        if actor is None:
            raise KnowbotTestUnconfigured("the test account has never signed in")
        root = self.config.workspace_root
        env = next(
            (
                e
                for e in await repo.list_environments(owner_actor_id=actor)
                if root in list(e.get("roots") or [])
            ),
            None,
        )
        if env is None:
            raise KnowbotTestUnconfigured("the test computer is not paired")
        # 每次测试一个项目，目录是工作区下这次测试自己的子目录（账 129）
        project = await repo.find_project(
            owner_actor_id=actor,
            environment_id=str(env["id"]),
            workspace_root=root,
            path=folder,
        )
        if project is None:
            async with repo.transaction():
                project_id = await repo.create_project(
                    owner_actor_id=actor,
                    environment_id=str(env["id"]),
                    workspace_root=root,
                    path=folder,
                    title="分身测试",
                )
        else:
            project_id = str(project["id"])
        others = tuple(
            str(e["name"])
            for e in await repo.list_environments(owner_actor_id=actor)
            if str(e["id"]) != str(env["id"]) and e.get("status") == ONLINE
        )
        return Account(
            actor_id=actor,
            environment_id=str(env["id"]),
            project_id=project_id,
            online=env.get("status") == ONLINE,
            others_online=others,
        )

    async def advance(self, run_id: str) -> None:
        async with self.stores() as repo:
            run = await repo.get_test_run(run_id)
            if run is None or run["status"] not in ACTIVE:
                return
            now = self.now()
            # 隔离打开时每次测试用自己的目录；没打开（测试电脑的代理还不支持建目录）就在
            # 工作区本身里跑，结果里写明没有隔离（账 129，所有者 2026-10-18 选甲）
            folder = run_folder(run) if self.folders is not None else ""
            try:
                account = await self._account(repo, folder)
            except KnowbotTestUnconfigured as exc:
                await self._finish(repo, run, status="error", error=exc.message)
                return
            if run["started_at"] is None:
                if not account.online:
                    # 测试电脑不在线不开跑，免得委托落到测试账号下别的电脑上（账 133）；过了时限报错
                    if (now - run["created_at"]).total_seconds() > run[
                        "timeout_seconds"
                    ]:
                        error = "the test computer is offline"
                        if account.others_online:
                            error += (
                                "; online on the test account instead: "
                                + ", ".join(account.others_online)
                            )
                        await self._finish(repo, run, status="error", error=error)
                    return
                try:
                    ready = self.folders is None or await self.folders.prepare(
                        owner=account.actor_id,
                        root=str(self.config.workspace_root),
                        folder=folder,
                    )
                except FolderRefused as exc:
                    await self._finish(
                        repo,
                        run,
                        status="error",
                        error="the test computer refused to prepare a clean folder: "
                        + exc.message,
                    )
                    return
                if not ready:
                    # 测试电脑没法给出一个干净的目录（离线、删不掉别人的）：不开跑，下一轮再试；
                    # 等过了这次测试的时限还不行，就报错结束，不一直挂着
                    waited = (now - run["created_at"]).total_seconds()
                    if waited > run["timeout_seconds"]:
                        await self._finish(
                            repo,
                            run,
                            status="error",
                            error="the test computer could not prepare a clean folder",
                        )
                    return
                async with repo.transaction():
                    await repo.update_test_run(run_id, status="running", started_at=now)
                run = await repo.get_test_run(run_id)
                assert run is not None
            request = _request(run)
            cases = await repo.list_test_cases(run_id)
            current = next((c for c in cases if not c.get("result")), None)
            waited_now = (
                int((now - current["waiting_since"]).total_seconds())
                if current is not None and current.get("waiting_since")
                else 0
            )
            deadline = run["started_at"] + timedelta(
                seconds=run["timeout_seconds"]
                + int(run["waited_seconds"] or 0)
                + waited_now
            )
            if now > deadline:
                await self._time_out(repo, run, request, cases, account)
                return
            if current is None:
                await self._conclude(repo, run, request, cases)
                return
            case = next(c for c in request.cases if c.case_id == current["case_id"])
            if not current.get("task_id"):
                await self._start_case(repo, run, request, case, len(cases), account)
                return
            await self._follow_case(repo, run, case, current, account)

    async def _start_case(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        request: RunRequest,
        case: RunCase,
        total: int,
        account: Account,
    ) -> None:
        manifest = request.manifest
        pack = knowbots.pack_from_manifest(
            manifest,
            knowbot_id=NO_KNOWBOT,
            version=str(manifest.get("version")),
            profile=trial_profile_id(request.run_id),
        )
        per_case = (Decimal(request.budget.limit) / total).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        started = await SessionService(repo).start(
            owner_actor_id=account.actor_id,
            kind="expert",
            project_id=account.project_id,
            title=f"分身测试 {case.case_id}",
        )
        session_id = started["session_id"]
        made = await Ledger(repo).handover(
            HandoverRequest(
                idempotency_key=(
                    "test-" + trial_profile_id(request.run_id)[13:] + "-" + case.case_id
                )[:128],
                session_id=session_id,
                profile_id=pack.profile_id,
                original_input={"text": case.input.question},
                budget_limit=per_case if per_case > 0 else None,
                client_context={"entry": "knowbot-test", "run_id": request.run_id},
            ),
            owner_actor_id=account.actor_id,
            snapshot=PinnedPack(
                pack=pack, digest=knowbots.manifest_digest(manifest), source=SOURCE_TEST
            ),
        )
        session = await repo.get_session(session_id)
        async with repo.transaction():
            if not made.get("queued_behind"):
                await repo.enqueue_command(
                    session_id=session_id,
                    sandbox_id=str(session["sandbox_id"]),
                    kind="task.drive",
                    payload={"task_id": made["task_id"]},
                )
            await repo.update_test_case(
                run["run_id"],
                case.case_id,
                project_id=account.project_id,
                session_id=session_id,
                task_id=made["task_id"],
            )

    async def _follow_case(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        case: RunCase,
        row: dict[str, Any],
        account: Account,
    ) -> None:
        task = await repo.get_task(str(row["task_id"]))
        now = self.now()
        if TaskState(task["state"]) in TASK_TERMINAL:
            await self._record(repo, run, case, row, task, account)
            return
        if behind := queued_behind(task):
            # 排在测试账号别的委托后面（同一项目一次一位）：排队不算进超时，但排到这次测试的时限
            # 还没轮到就按超时收，原因写清排在谁后面（0034 §三.3）
            if row.get("waiting_since") is None:
                async with repo.transaction():
                    await repo.update_test_case(
                        run["run_id"], case.case_id, waiting_since=now
                    )
            elif (now - row["waiting_since"]).total_seconds() > run["timeout_seconds"]:
                await self._stop_waiting(repo, run, row, now)
                await self._time_out(
                    repo,
                    run,
                    _request(run),
                    await repo.list_test_cases(run["run_id"]),
                    account,
                    error=f"queued too long behind task {behind}",
                )
            return
        if task["state"] != TaskState.WAITING or not task.get("active_interaction_id"):
            await self._stop_waiting(repo, run, row, now)
            return
        interaction = await repo.get_interaction(str(task["active_interaction_id"]))
        if interaction["kind"] not in ("tool_approval", "question"):
            # 验收不过、返工用完要交人，或别的要人拍板的：测试里不让人放过，记 failed
            subject = (interaction.get("prompt") or {}).get("subject") or {}
            note = f"stopped for a decision in the workbench ({interaction['kind']})"
            if subject.get("out_of_scope"):
                # 受理关口（账 130）：专家认为用例的问题不归它。多半是用例问错了，不是运行出错
                note = (
                    "the expert judged the question out of its scope: "
                    f"{subject.get('reason') or '(no reason given)'}"
                )
            await cancel_task(repo, str(task["id"]), owner_actor_id=account.actor_id)
            await self._record(
                repo,
                run,
                case,
                row,
                await repo.get_task(str(task["id"])),
                account,
                note=note,
            )
            return
        try:
            answered = await self._respond_if_ready(
                repo, run, case, interaction, account
            )
        except InteractionRejected as exc:
            # 答不进去（那一轮已经结束、问题失效）：记一次原因，不让整次测试每 5 秒报错一遍；
            # 委托由推它的程序收拾（0034 后补：那一轮结束时作废它里面的问题）
            if str(interaction["id"]) not in self._unanswerable:
                self._unanswerable.add(str(interaction["id"]))
                log.warning(
                    "knowbot test could not answer run=%s case=%s interaction=%s: %s",
                    run["run_id"],
                    case.case_id,
                    interaction["id"],
                    exc.message,
                )
            return
        if answered:
            await self._stop_waiting(repo, run, row, now)
            return
        if row.get("waiting_since") is None:
            async with repo.transaction():
                await repo.update_test_case(
                    run["run_id"], case.case_id, waiting_since=now
                )
            return
        if (now - row["waiting_since"]).total_seconds() > run["max_wait_seconds"]:
            await cancel_task(repo, str(task["id"]), owner_actor_id=account.actor_id)
            await self._stop_waiting(repo, run, row, now)
            await self._record(
                repo,
                run,
                case,
                row,
                await repo.get_task(str(task["id"])),
                account,
                needs_human=True,
            )

    async def _respond_if_ready(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        case: RunCase,
        interaction: dict[str, Any],
        account: Account,
    ) -> bool:
        """这个交互的问题都有答案了（预给的或 knowlab 交来的）：代测试账号回答。"""
        iid = str(interaction["id"])
        subject = (interaction.get("prompt") or {}).get("subject") or {}
        if interaction["kind"] == "tool_approval":
            given = await repo.get_test_answer(run["run_id"], iid)
            if given is None:
                return False
            decision = (
                DECISION_ACCEPT
                if given["answer"].get("decision") == "approve"
                else DECISION_DECLINE
            )
            response: dict[str, Any] = {"decision": decision}
        else:
            answers: dict[str, str] = {}
            for q in subject.get("questions") or []:
                qid = str(q.get("id"))
                labels = {
                    str(o.get("id") or o.get("label")): str(o.get("label") or "")
                    for o in q.get("options") or []
                }
                given = await repo.get_test_answer(run["run_id"], f"{iid}:{qid}")
                if given is not None:
                    chosen = given["answer"].get("option_id")
                    text = given["answer"].get("text")
                    answers[qid] = text or labels.get(str(chosen), str(chosen or ""))
                elif subject.get("ask_before") and qid in case.input.answers:
                    value = case.input.answers[qid]
                    answers[qid] = labels.get(value, value)
                else:
                    return False
            response = {"answers": answers}
            decision = None
        token = await repo.interaction_token(
            session_id=str(interaction["session_id"]), interaction_id=iid
        )
        if token is None:
            return False
        await answer_interaction(
            repo,
            iid,
            token=token,
            response=response,
            owner_actor_id=account.actor_id,
            decision=decision,
        )
        return True

    async def _stop_waiting(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        row: dict[str, Any],
        now: datetime,
    ) -> None:
        """不再等人了：这段等待记进已等时间（不计入超时）。"""
        if row.get("waiting_since") is None:
            return
        waited = max(0, int((now - row["waiting_since"]).total_seconds()))
        async with repo.transaction():
            await repo.update_test_case(
                run["run_id"],
                row["case_id"],
                waiting_since=None,
                waited_seconds=int(row.get("waited_seconds") or 0) + waited,
            )
            await repo.update_test_run(
                run["run_id"], waited_seconds=int(run["waited_seconds"] or 0) + waited
            )

    async def _record(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        case: RunCase,
        row: dict[str, Any],
        task: dict[str, Any],
        account: Account,
        *,
        needs_human: bool = False,
        note: str | None = None,
    ) -> None:
        view = await ExpertDesk(repo).steps(
            str(task["id"]), owner_actor_id=account.actor_id
        )
        artifacts = {
            a["name"]: a["content"]
            for a in await repo.list_artifacts_with_content(str(task["id"]))
            if a.get("kind") == "step"
        }
        result = case_result(
            case=case,
            task=task,
            steps=view["steps"],
            artifacts=artifacts,
            needs_human=needs_human,
            reason=view["task"].get("reason"),
        )
        if note and result["status"] != "passed":
            result["note"] = note[:1000]
        if self.folders is None:
            shared = "本次测试没有隔离：测试电脑的本地代理还不支持建、删目录，在共用的工作区里跑"
            result["note"] = (
                f"{result['note']}；{shared}" if result.get("note") else shared
            )[:1000]
        # 排查不用翻库：哪次测试、哪条用例、哪一步、为什么没过（审读第 3 条）
        for line in result["steps"]:
            for detail in line.get("details") or []:
                log.info(
                    "knowbot test check failed run=%s case=%s step=%s detail=%s",
                    run["run_id"],
                    case.case_id,
                    line["step_id"],
                    detail,
                )
        outputs = await self._outputs(
            repo, run, case, task, account, steps=len(view["steps"])
        )
        spent = Decimal(str((task.get("budget") or {}).get("used") or "0"))
        async with repo.transaction():
            await repo.update_test_case(
                run["run_id"], case.case_id, result=result, outputs=outputs
            )
            await repo.update_test_run(
                run["run_id"], spent=Decimal(str(run["spent"])) + spent
            )

    async def _outputs(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        case: RunCase,
        task: dict[str, Any],
        account: Account,
        *,
        steps: int,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        """这条用例的成稿与各步交回物（SDD 0037）。取不到的照实写，不影响结论。"""
        task_id = str(task["id"])
        try:
            desk = ExpertDesk(repo)
            full = [
                (await desk.step(task_id, n, owner_actor_id=account.actor_id))["step"]
                for n in range(1, steps + 1)
            ]
            deliverable, note = await self._deliverable(repo, task, full)
            if deliverable is not None and timed_out:
                note = "超时收尾时取的，可能没写完"
            return case_outputs(
                run_id=run["run_id"],
                case_id=case.case_id,
                steps=full,
                deliverable=deliverable,
                deliverable_note=note,
            )
        except Exception:  # noqa: BLE001 - 产出取不到不拖累结论
            log.exception(
                "knowbot test outputs failed run=%s case=%s",
                run["run_id"],
                case.case_id,
            )
            return case_outputs(
                run_id=run["run_id"],
                case_id=case.case_id,
                steps=[],
                deliverable=None,
                deliverable_note=None,
                error="取产出时平台出了错，这条用例的成稿与交回物没取到",
            )

    async def _deliverable(
        self, repo: WorkbenchStore, task: dict[str, Any], steps: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """只读这条用例验收时记下了路径的那份成稿（每条用例的底稿在自己那一格）；文件的修改
        时间早于这条用例开始的不认，不是这次写的（审读 10-19 第 3 条）。"""
        written = sorted(
            (
                a
                for a in await repo.list_artifacts_with_content(str(task["id"]))
                if a.get("kind") == "step" and a.get("workspace_path")
            ),
            key=lambda a: str(a.get("created_at") or ""),
        )
        if not written:
            return None, "这条用例没有写成稿：写稿那一步没走到，或没写文件"
        path = str(written[-1]["workspace_path"])
        step_id = next(
            (
                str(s.get("step_id"))
                for s in steps
                if s.get("returns") == written[-1].get("name")
            ),
            None,
        )
        if self.reader is None:
            return None, "没有读测试电脑文件的通道（会合点没配），成稿没取"
        session = await repo.get_session(str(task["session_id"]))
        try:
            found = await self.reader.read(
                owner=str(task["owner_actor_id"]),
                directory=session.get("project_root"),
                path=path,
            )
        except DeliverableTooLarge as exc:
            return None, f"成稿太大没取（{exc.details.get('size')} 字节）"
        except WorkbenchError as exc:
            return None, f"成稿没读到：{exc.code}"
        if found is None:
            return None, "成稿没读到：问不到测试电脑"
        started = task.get("created_at")
        if (
            found.mtime_ms is not None
            and hasattr(started, "timestamp")
            and found.mtime_ms < started.timestamp() * 1000 - 1000
        ):
            return None, "成稿文件早于这条用例开始，不是这次写的"
        return {
            "path": path,
            "text": found.text,
            "truncated": False,
            **({"step_id": step_id} if step_id else {}),
        }, None

    async def _time_out(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        request: RunRequest,
        cases: list[dict[str, Any]],
        account: Account,
        *,
        error: str = "timed out",
    ) -> None:
        for row in cases:
            if row.get("result"):
                continue
            case = next(c for c in request.cases if c.case_id == row["case_id"])
            outputs = None
            if row.get("task_id"):
                task = await repo.get_task(str(row["task_id"]))
                if TaskState(task["state"]) not in TASK_TERMINAL:
                    await cancel_task(
                        repo, str(task["id"]), owner_actor_id=account.actor_id
                    )
                # 超时的也留下做到哪、交回了什么：排查第 1 稿那种全超时最要这个（SDD 0037）
                view = await ExpertDesk(repo).steps(
                    str(task["id"]), owner_actor_id=account.actor_id
                )
                outputs = await self._outputs(
                    repo,
                    run,
                    case,
                    task,
                    account,
                    steps=len(view["steps"]),
                    timed_out=True,
                )
            async with repo.transaction():
                await repo.update_test_case(
                    run["run_id"],
                    case.case_id,
                    result=case_result(
                        case=case, task=None, steps=[], artifacts={}, error=error
                    ),
                    outputs=outputs,
                )
        await self._finish(
            repo, run, status="timeout", error=None if error == "timed out" else error
        )

    async def _conclude(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        request: RunRequest,
        cases: list[dict[str, Any]],
    ) -> None:
        statuses = [(c.get("result") or {}).get("status") for c in cases]
        if all(s == "passed" for s in statuses):
            status = "passed"
        elif "failed" not in statuses and "error" in statuses:
            status = "error"  # 没有一条是数不对，是平台没能验（审读第 3 条）
        else:
            status = "failed"
        await self._finish(repo, run, status=status)

    async def _finish(
        self,
        repo: WorkbenchStore,
        run: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        now = self.now()
        async with repo.transaction():
            await repo.update_test_run(
                run["run_id"],
                status=status,
                error=error,
                finished_at=now,
                cleanup_after=now + KEEP,
            )
        # 排查不用翻库：测试怎么结束的（qwen-007 走查：出错结束时 runner 一行日志都没有）
        log.info(
            "knowbot test run finished run=%s status=%s error=%s",
            run["run_id"],
            status,
            error or "",
        )
        # 测完就删这次的目录（账 129）；删不掉的，下一个测试开跑前还会再删
        if self.folders is not None and self.config.workspace_root:
            actor = await repo.actor_for_subject(str(self.config.subject))
            if actor is not None:
                await self.folders.remove(
                    owner=actor,
                    root=self.config.workspace_root,
                    folder=run_folder(run),
                )

    async def run_forever(self, stop: Any, interval_seconds: float) -> None:
        """runner 进程里的一条线：隔一会儿推一次在跑的测试、清一次到期的产出。"""
        import asyncio

        log.info("knowbot test conductor starting interval=%ss", interval_seconds)
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("knowbot test conductor iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass

    async def cleanup(self) -> int:
        """测试产出保留期满：删委托、对话、交回物；取好的产出 30 天后清空；测试结果留着。"""
        async with self.stores() as repo:
            expired = await repo.expired_test_runs(self.now())
            for run in expired:
                async with repo.transaction():
                    await repo.purge_test_run(run["run_id"])
            # 过期产出每小时清一次就够（审读 10-19）
            now = self.now()
            last = self._outputs_cleared_at
            if last is None or now - last >= timedelta(hours=1):
                async with repo.transaction():
                    await repo.clear_test_outputs(now - KEEP_OUTPUTS)
                self._outputs_cleared_at = now
        return len(expired)
