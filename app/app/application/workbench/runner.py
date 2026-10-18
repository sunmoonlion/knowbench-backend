"""runner：第五个进程角色。对每个沙箱保持一条到 app-server 的连接；把通知投影成 Session 事件；
执行网页接口排进 workbench_commands 的命令；把 app-server 的审批请求按方向盘路由。

它不做业务判断：状态、方向盘、Interaction 全经账房（Ledger / SessionService）；这里只是连接与搬运。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.application.ports.workbench import (
    AppServerConnection,
    AppServerConnector,
    AppServerError,
    EventPublisher,
    KnowbotDataSource,
    WorkbenchStore,
    WorkbenchStores,
)
from app.application.workbench.advisor import Advisor, TurnResult
from app.application.workbench.catalog import pack_for_task
from app.application.workbench.deliverable_check import DeliverableCheck
from app.application.workbench.ledger import Ledger
from app.domain.workbench import missing_data
from app.domain.workbench.deliverables import changed_paths
from app.domain.workbench.memory import memory_note
from app.domain.workbench.models import InteractionPrompt
from app.domain.workbench.pricing import (
    DEFAULT_PRICES,
    PriceList,
    Usage,
    cost_of,
    cumulative_of,
    fresh_usage,
)
from app.domain.workbench.projects import (
    THREAD_CONFIG,
    mode_note,
    mode_of,
    orchestrator_cwd,
    thread_settings,
    turn_settings,
)
from app.domain.workbench.states import TaskState, WaitingReason, Wheel
from app.domain.workbench.tool_policy import (
    DECISION_ACCEPT,
    DECISION_ACCEPT_TASK,
    DECISION_CANCEL,
    DECISION_DECLINE,
    classify,
    decide,
    dependents,
    grant_matches,
    grant_prefix,
)

log = logging.getLogger(__name__)

# 不落库的流式通知（条目级事件才进 Session 事件，persistence.md）
# 增量不入账：成段的内容在 item/completed 里。0.155.1 的名字有 …/delta、…/outputDelta、…/summaryTextDelta 等
_SKIP_SUFFIXES = ("/delta", "Delta")
_KIND_BY_PREFIX = (
    ("thread/environment/", "environment"),
    ("thread/tokenUsage", "usage"),
    ("thread/", "thread"),
    ("turn/", "turn"),
    ("item/", "item"),
    ("error", "error"),
)


def event_kind(method: str) -> str:
    for prefix, kind in _KIND_BY_PREFIX:
        if method.startswith(prefix):
            return kind
    return "other"


# app-server 说"这条线程没了"的两种文案：内存里没有（turn/start）与盘上没有 rollout（thread/resume，
# Codex 0.155 thread-store/local/read_thread.rs）。KIND 14：只认前者，后者没走兜底，命令直接失败
_THREAD_GONE = ("thread not found", "no rollout found")


def thread_gone(exc: AppServerError) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in _THREAD_GONE)


def thread_id_of(params: dict[str, Any]) -> str | None:
    for path in (
        ("threadId",),
        ("thread", "id"),
        ("turn", "threadId"),
        ("item", "threadId"),
    ):
        cur: Any = params
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, str):
            return cur
    return None


@dataclass(frozen=True)
class TurnPlan:
    """这一轮怎么发：四样设置，以及这一轮属于哪种情形。"""

    settings: dict[str, Any] = field(default_factory=dict)
    mode: str | None = None
    directory: str | None = None
    session_id: str | None = None
    # 关于这位用户的几条事实（长期记忆，0019 §四）：和情形说明一起插进线里，变了就再插一次
    facts: str | None = None

    @property
    def signature(self) -> str | None:
        if self.mode is None:
            return None
        digest = hashlib.sha256((self.facts or "").encode()).hexdigest()[:12]
        return f"{self.mode}|{self.directory or ''}|{digest}"


class SandboxLink:
    """一个沙箱一条连接。"""

    def __init__(self, runner: Runner, sandbox: dict[str, Any]):
        self.runner = runner
        self.sandbox = sandbox
        self.sandbox_id = str(sandbox["id"])
        self.client: AppServerConnection | None = None
        self.threads: dict[str, str] = {}  # thread_id -> session_id
        # 当前这条连接对面的 app-server 进程已经装载的线程。沙箱回收再拉起是新进程，盘上的 rollout 还在、
        # 内存里没有：turn/start 前要先 thread/resume（KIND 2026-09-26 第 13 轮：401 修好后改报 thread not found）
        self.live_threads: set[str] = set()
        # 每条线上一次向模型说明的情形。进程重启后会再说一次，多说一次无妨
        self.announced: dict[str, str] = {}
        # 每条线用的是哪个模型（起线、重新装载线时对面告诉我们的）：算钱要按它的单价
        self.models: dict[str, str] = {}
        # 每条线正在跑的那一轮：用户点停止时要知道停哪一轮
        self.active_turns: dict[str, str] = {}
        # 正在跑的各轮到现在的用量：每调用一次模型加一次
        self.turn_usage: dict[str, Usage] = {}
        # 每条线上一次报的累计。None：这条线还没报过
        self.totals: dict[str, Usage | None] = {}
        self.pending_approvals: dict[
            str, asyncio.Future
        ] = {}  # request_id -> future(response dict: decision / answer / answers)
        # 等人答的那一问属于哪条线：专家被取消时，只放掉这条线上的（同一台机器上别的对话不动）
        self.pending_threads: dict[str, str] = {}
        # 被「取消委托」放掉的那几问：它们不再等人，也不再去关待办（待办已随委托关掉）
        self.released: set[str] = set()
        self._lock = asyncio.Lock()
        self.turn_waiters: dict[str, dict[str, Any]] = {}  # turn_id -> waiter
        # 用户在审批面点了「停下」（或等不到人）：这条线这一轮是被人停的，不是出错。
        # thread_id -> {"note": 用户附的话, "source": user / timeout}
        self.user_stops: dict[str, dict[str, Any]] = {}

    async def ensure_connected(self) -> AppServerConnection:
        async with self._lock:
            if self.client is not None and self.client.connected:
                return self.client
            client = self.runner.connector.open(
                self.sandbox,
                on_notification=self.on_notification,
                on_server_request=self.on_server_request,
            )
            await client.connect()
            self.client = client
            self.live_threads.clear()
            log.info(
                "sandbox link up sandbox=%s url=%s",
                self.sandbox_id,
                self.sandbox["app_server_url"],
            )
            return client

    async def ensure_thread(self, thread_id: str) -> None:
        """让对面的 app-server 装载这个线程：新进程从盘上 resume；已在跑的线程 resume 等于重新加入，无副作用。
        rollout 不在了（换过持久卷等）会抛 AppServerError（thread not found），由调用方决定要不要另起线程。"""
        if thread_id in self.live_threads:
            return
        client = await self.ensure_connected()
        resumed = await client.request(
            "thread/resume",
            {"threadId": thread_id, "config": dict(THREAD_CONFIG)},
            timeout=60,
        )
        self.remember_model(thread_id, resumed)
        self.live_threads.add(thread_id)
        log.info("thread resumed sandbox=%s thread=%s", self.sandbox_id, thread_id)

    def remember_model(self, thread_id: str, answer: Any) -> str | None:
        """起线、重新装载线的答复里有这条线用的模型。"""
        if isinstance(answer, dict):
            model = answer.get("model") or (answer.get("thread") or {}).get("model")
            if isinstance(model, str) and model:
                self.models[thread_id] = model
        return self.models.get(thread_id)

    async def recall_model(
        self, thread_id: str | None, session_id: str, repo: WorkbenchStore
    ) -> None:
        """进程重启过、对面又没再说的时候：从账里找这条线起的时候记下的模型。"""
        if not thread_id or thread_id in self.models:
            return
        started = await repo.list_record_events(
            session_id=session_id, types=("session/thread_started",)
        )
        for event in reversed(started):
            if event["payload"].get("thread_id") == thread_id:
                self.models[thread_id] = str(event["payload"].get("model") or "")
                return
        self.models[thread_id] = ""

    async def recall_total(
        self, thread_id: str | None, session_id: str, repo: WorkbenchStore
    ) -> None:
        """进程重启过的时候：从账里找这条线上一次报的累计。"""
        if not thread_id or thread_id in self.totals:
            return
        self.totals[thread_id] = None
        reported = await repo.list_record_events(
            session_id=session_id, types=("thread/tokenUsage/updated",)
        )
        for event in reversed(reported):
            if thread_id_of(event["payload"]) == thread_id:
                self.totals[thread_id] = cumulative_of(
                    event["payload"].get("tokenUsage")
                )
                return

    def priced(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """用量通知进账之前，带上按价目表算好的金额。页面照着显示，不自己算价。

        `call` 是这一条通知带来的新花费，`turn` 是这一轮到现在花的。不认识这个模型的
        单价时金额是空的，只有用量。返回 None：这一条是重复报的，没有新用量，不进账。
        """
        if not method.startswith("thread/tokenUsage"):
            return params
        reported = params.get("tokenUsage") or params.get("usage")
        thread_id = thread_id_of(params)
        usage = fresh_usage(reported, self.totals.get(thread_id or ""))
        if thread_id and cumulative_of(reported) is not None:
            self.totals[thread_id] = cumulative_of(reported)
        if usage.total == 0:
            return None
        turn_id = params.get("turnId") or self.active_turns.get(thread_id or "")
        so_far = usage
        if turn_id:
            so_far = self.turn_usage.get(turn_id, Usage()) + usage
            self.turn_usage[turn_id] = so_far
        prices = self.runner.prices
        model = self.models.get(thread_id or "") or None
        price = prices.of(model)
        call, turn = cost_of(usage, price), cost_of(so_far, price)
        return {
            **params,
            "cost": {
                "currency": prices.currency,
                "model": model,
                "priced": price is not None,
                "call": None if call is None else str(call),
                "turn": None if turn is None else str(turn),
                "call_tokens": usage.as_json(),
                "turn_tokens": so_far.as_json(),
                "estimated": True,
            },
        }

    def track_turn(self, method: str, params: dict[str, Any]) -> None:
        thread_id = thread_id_of(params)
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if not thread_id or not turn_id:
            return
        if method == "turn/started":
            self.active_turns[thread_id] = turn_id
        elif method == "turn/completed":
            if self.active_turns.get(thread_id) == turn_id:
                self.active_turns.pop(thread_id, None)
            if turn_id not in self.turn_waiters:  # 专家的那一轮由 run_turn 自己收
                self.turn_usage.pop(turn_id, None)

    async def announce(self, thread_id: str, plan: TurnPlan) -> None:
        """情形换了，就往线里插一段说明，再发这一轮。说明进账：模型看到过什么，账里要有。

        插不进去不拦这一轮：四样设置照样随这一轮发出，权限不受影响，只是模型可能不知道自己手里有什么。
        """
        signature = plan.signature
        if signature is None or self.announced.get(thread_id) == signature:
            return
        assert plan.mode is not None
        note = mode_note(plan.mode, directory=plan.directory)
        if plan.facts:
            note = f"{note}\n\n{plan.facts}"
        client = await self.ensure_connected()
        injected = True
        try:
            await client.request(
                "thread/inject_items",
                {
                    "threadId": thread_id,
                    "items": [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": note}],
                        }
                    ],
                },
                timeout=30,
            )
        except AppServerError as exc:
            injected = False
            log.warning(
                "mode note not injected sandbox=%s thread=%s: %s",
                self.sandbox_id,
                thread_id,
                exc,
            )
        self.announced[thread_id] = signature
        if plan.session_id is None:
            return
        async with self.runner.stores() as repo:
            async with repo.transaction():
                event = await repo.append_event(
                    session_id=plan.session_id,
                    kind="session",
                    event_type="session/mode_announced",
                    payload={
                        "mode": plan.mode,
                        "directory": plan.directory,
                        "injected": injected,
                        "note": note,
                    },
                )
            await self.runner.publisher.publish(
                self.runner.publisher.session_channel(plan.session_id), event
            )

    async def session_for_thread(
        self, thread_id: str | None, repo: WorkbenchStore
    ) -> str | None:
        if thread_id is None:
            return None
        sid = self.threads.get(thread_id)
        if sid is None:
            row = await repo.get_session_by_thread(thread_id)
            if row is not None:
                sid = str(row["id"])
                self.threads[thread_id] = sid
        return sid

    # ---- 通知 → Session 事件 ----
    async def on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method.endswith(_SKIP_SUFFIXES):
            return
        self.track_turn(method, params)
        async with self.runner.stores() as repo:
            session_id = await self.session_for_thread(thread_id_of(params), repo)
            if session_id is None:
                log.debug("notification without session method=%s", method)
                return
            if method.startswith("thread/tokenUsage"):
                await self.recall_model(thread_id_of(params), session_id, repo)
                await self.recall_total(thread_id_of(params), session_id, repo)
                priced = self.priced(method, params)
                if priced is None:
                    log.debug("usage reported again, not counted method=%s", method)
                    return
                params = priced
            async with repo.transaction():
                event = await repo.append_event(
                    session_id=session_id,
                    kind=event_kind(method),
                    event_type=method,
                    payload=params,
                )
            await self.runner.publisher.publish(
                self.runner.publisher.session_channel(session_id), event
            )
            if method == "item/completed":
                await self._note_missing_data(repo, session_id, params.get("item"))
        self._feed_turn_waiters(method, params)

    async def _note_missing_data(
        self, repo: WorkbenchStore, session_id: str, item: Any
    ) -> None:
        """查数据的工具答复没有这个数据集、又认得出证券代码：记一条「没有数据」（F-PROJ-07）。"""
        missing = missing_data.from_tool_call(item)
        if missing is None:
            return
        async with repo.transaction():
            event = await repo.append_event_once(
                session_id=session_id,
                kind="data",
                event_type=missing_data.EVENT,
                payload=missing,
                same={"security_code": missing["security_code"]},
            )
        if event is not None:
            await self.runner.publisher.publish(
                self.runner.publisher.session_channel(session_id), event
            )

    def _feed_turn_waiters(self, method: str, params: dict[str, Any]) -> None:
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if method in (
            "thread/environment/disconnected",
            "thread/environment/connected",
        ):
            thread_id = thread_id_of(params)
            if thread_id is None:
                return
            for w in self.turn_waiters.values():
                if w.get("thread_id") != thread_id:
                    continue
                if method == "thread/environment/disconnected":
                    w["env_lost"] = True
                    w["error"] = "execution environment disconnected"
                elif w["env_lost"]:
                    # 短断在当前 turn 结束前恢复：让它继续，不把成功的 turn 误判为丢失。
                    w["env_lost"] = False
                    if w["error"] == "execution environment disconnected":
                        w["error"] = None
            return
        if method == "error":
            for w in self.turn_waiters.values():
                w["error"] = str(params.get("message") or params)
            return
        w = self.turn_waiters.get(turn_id) if turn_id else None
        if w is None:
            return
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and item.get("text"):
                w["text"] = item["text"]
            # 这一轮用补丁改到的文件：成稿有没有写出来就看它（0015 §3.3）
            w["files"].extend(changed_paths(item))
        elif method.startswith("thread/tokenUsage"):
            w["reported"] = True
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("status") not in (None, "completed") and not w.get("error"):
                w["error"] = f"turn {turn.get('status')}"
                w["interrupted"] = turn.get("status") == "interrupted"
            if not w["future"].done():
                w["future"].set_result(True)

    async def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        timeout: float = 900,  # noqa: ASYNC109
    ) -> TurnResult:  # noqa: ASYNC109
        """TurnDriver：顾问在用户的 thread 上发一个 turn 并等它结束。"""
        client = await self.ensure_connected()
        await self.ensure_thread(thread_id)
        plan = await self.runner.turn_plan(thread_id=thread_id, expert=True)
        await self.announce(thread_id, plan)
        result = await client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                **plan.settings,
            },
            timeout=120,
        )
        turn_id = ((result or {}).get("turn") or {}).get("id")
        if not turn_id:
            return TurnResult(
                turn_id=None,
                final_text=None,
                tokens={},
                error=f"turn/start returned no turn id: {result}",
            )
        waiter: dict[str, Any] = {
            "thread_id": thread_id,
            "future": asyncio.get_running_loop().create_future(),
            "text": None,
            "reported": False,
            "error": None,
            "env_lost": False,
            "interrupted": False,
            "files": [],
            "timed_out": False,
        }
        self.turn_waiters[turn_id] = waiter
        self.active_turns[thread_id] = turn_id
        try:
            await asyncio.wait_for(waiter["future"], timeout)
            # 真 app-server 的 thread/tokenUsage 可能晚于 turn/completed 到达：留一小段宽限期再撤 waiter
            for _ in range(20):
                if waiter["reported"]:
                    break
                await asyncio.sleep(0.1)
        except TimeoutError:
            waiter["error"] = "turn timed out"
            waiter["timed_out"] = True
            try:
                await client.request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    timeout=30,
                )
            except Exception:  # noqa: BLE001
                pass
        except ConnectionError as exc:
            waiter["error"] = f"connection lost: {exc}"
        finally:
            self.turn_waiters.pop(turn_id, None)
            if self.active_turns.get(thread_id) == turn_id:
                self.active_turns.pop(thread_id, None)
        # 这一轮花的 = 这一轮里每次调用相加。不拿这条线的累计来算
        used = self.turn_usage.pop(turn_id, Usage())
        stop = self.user_stops.pop(thread_id, None)
        return TurnResult(
            turn_id=turn_id,
            final_text=waiter["text"],
            tokens=used.as_json(),
            error=waiter["error"],
            environment_lost=waiter["env_lost"],
            cost=cost_of(
                used, self.runner.prices.of(self.models.get(thread_id) or None)
            ),
            interrupted=waiter["interrupted"] or stop is not None,
            stopped_by_user=stop is not None,
            stop_note=(stop or {}).get("note"),
            files=list(waiter["files"]),
            timed_out=waiter["timed_out"],
        )

    async def interrupt(
        self, thread_id: str | None, turn_id: str | None = None
    ) -> str | None:
        """停下这条线正在跑的那一轮。没有在跑的就什么都不做。返回停的是哪一轮。"""
        turn_id = turn_id or self.active_turns.get(thread_id or "")
        if not thread_id or not turn_id:
            return None
        client = await self.ensure_connected()
        await self.ensure_thread(thread_id)
        await client.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=30
        )
        return turn_id

    # ---- MCP 询问（elicitation）：Codex 在调 MCP 工具前向客户端要一次确认（user verification）----
    async def _on_elicitation(self, rid: Any, params: dict[str, Any]) -> None:
        """表单式询问按 requestedSchema 用默认值答"accept"；url 式（OAuth）拒绝。

        我们只挂自己的知识 MCP（0006），令牌在沙箱配置里已经把它限定为可信；用户的第三方 MCP 第一期不接。
        """
        client = self.client
        assert client is not None
        mode = params.get("mode")
        log.info(
            "mcp elicitation sandbox=%s mode=%s message=%s meta=%s",
            self.sandbox_id,
            mode,
            str(params.get("message"))[:200],
            json.dumps(params.get("_meta"), ensure_ascii=False, default=str)[:300],
        )
        if mode == "url":
            await client.respond(rid, {"action": "decline"})
            return
        schema = params.get("requestedSchema") or {}
        content: dict[str, Any] = {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        for name, spec in (props or {}).items():
            if not isinstance(spec, dict):
                continue
            if "default" in spec and spec["default"] is not None:
                content[name] = spec["default"]
            elif spec.get("type") == "boolean":
                content[name] = True
            elif spec.get("enum"):
                content[name] = spec["enum"][0]
            elif spec.get("oneOf"):
                content[name] = spec["oneOf"][0].get("const")
            elif spec.get("type") in ("number", "integer"):
                content[name] = spec.get("minimum", 0)
            else:
                content[name] = ""
        await client.respond(rid, {"action": "accept", "content": content})

    # ---- 服务端请求：审批与提问，按方向盘和策略路由（approval.md 工具级）----
    async def on_server_request(
        self, rid: Any, method: str, params: dict[str, Any]
    ) -> None:
        client = self.client
        assert client is not None
        if method == "mcpServer/elicitation/request":
            await self._on_elicitation(rid, params)
            return
        if method == "item/tool/requestUserInput":
            await self._on_user_input(rid, params)
            return
        if not method.endswith("requestApproval"):
            await client.respond_error(
                rid, -32601, f"workbench does not handle {method}"
            )
            return
        request_id = f"{self.sandbox_id}:{rid}"
        category, tokens = classify(method, params)
        summary = {
            "method": method,
            "category": category,
            "command": params.get("command"),
            "tokens": list(tokens),
            "cwd": params.get("cwd"),
            "reason": params.get("reason"),
            "itemId": params.get("itemId"),
            "environmentId": params.get("environmentId"),
            "grantRoot": params.get("grantRoot"),
        }
        async with self.runner.stores() as repo:
            session_id = await self.session_for_thread(thread_id_of(params), repo)
            if session_id is None:
                await client.respond(rid, {"decision": "decline"})
                return
            session = await repo.get_session(session_id)
            where = await self._where(repo, session)
            wheel, task, pack, step = (
                where["wheel"],
                where["task"],
                where["pack"],
                where["step"],
            )
            task_id = str(task["id"]) if task else None
            step_id = step.step_id if step else None
            # 同一步里刚拒绝过：随后任何请求一律问人，并标出来
            after_decline = False
            if task_id and step_id:
                after_decline = any(
                    a["decision"] in ("decline", "cancel")
                    for a in await repo.list_approvals(task_id=task_id, step_id=step_id)
                )
            # 任务内授权（自驾：这段驾驶里）：用户以前点过「都允许」
            granted = any(
                grant_matches(g, category, tokens)
                for g in await repo.list_tool_grants(
                    session_id=session_id, task_id=task_id
                )
            )
            # 策略只对「顾问驾驶且找得到步骤」有；没有 Task、找不到专家包或步骤：问人
            tier = "ask"
            if wheel == Wheel.advisor and pack is not None and step is not None:
                tier = decide(
                    pack.policy_for(step), category, tokens, after_decline=after_decline
                )
            source = None
            if granted and not after_decline:
                tier, source = "allow", "task_grant"
            elif tier == "allow":
                source = "policy"
            log.info(
                "approval request sandbox=%s wheel=%s step=%s category=%s tier=%s granted=%s after_decline=%s",
                self.sandbox_id,
                wheel,
                step_id,
                category,
                tier,
                granted,
                after_decline,
            )
            if tier in ("allow", "deny"):
                decision = "accept" if tier == "allow" else "decline"
                async with repo.transaction():
                    await repo.log_approval(
                        session_id=session_id,
                        task_id=task_id,
                        request_id=request_id,
                        method=method,
                        summary=summary,
                        decision=decision,
                        source=source or "policy",
                        wheel=wheel,
                        step_id=step_id,
                    )
                    ev = await repo.append_event(
                        session_id=session_id,
                        kind="approval",
                        event_type="approval/policy",
                        payload={
                            "decision": decision,
                            "source": source or "policy",
                            "subject": summary,
                            "step_id": step_id,
                        },
                        task_id=task_id,
                        attempt_id=_opt_str((task or {}).get("active_attempt_id")),
                    )
                await self.runner.publisher.publish(
                    self.runner.publisher.session_channel(session_id), ev
                )
                await client.respond(rid, {"decision": decision})
                return
        await self._ask_human(
            rid,
            request_id=request_id,
            method=method,
            params=params,
            summary=summary,
            session=session,
            where=where,
            category=category,
            tokens=tokens,
            after_decline=after_decline,
        )

    async def _where(
        self, repo: WorkbenchStore, session: dict[str, Any]
    ) -> dict[str, Any]:
        """这条线现在谁在开、在哪个 Task 的哪一步。"""
        wheel = str(session["wheel"])
        task = pack = step = None
        if wheel == Wheel.advisor and session.get("active_task_id"):
            task = await repo.get_task(str(session["active_task_id"]))
            pack = pack_for_task(task)
            step = pack.step(int(task["current_step"])) if pack else None
        return {"wheel": wheel, "task": task, "pack": pack, "step": step}

    async def _open_ask(
        self,
        *,
        session: dict[str, Any],
        where: dict[str, Any],
        kind: str,
        prompt: InteractionPrompt,
        waiting_reason: WaitingReason,
    ) -> dict[str, Any]:
        """问人只有一条路：开 Interaction。顾问驾驶时 Task 同一提交进 WAITING；自驾只开待办。"""
        session_id = str(session["id"])
        task = where["task"]
        async with self.runner.stores() as repo:
            if task is not None:
                opened = await Ledger(repo).open_interaction(
                    str(task["id"]),
                    kind=kind,
                    prompt=prompt,
                    waiting_reason=waiting_reason,
                    attempt_id=_opt_str(task.get("active_attempt_id")),
                )
                ev = opened["event"]
            else:
                token = secrets.token_urlsafe(32)
                async with repo.transaction():
                    iid = await repo.insert_interaction(
                        session_id=session_id,
                        task_id=None,
                        attempt_id=None,
                        kind=kind,
                        prompt=prompt.model_dump(),
                        subject_digest=None,
                        token=token,
                        target_state_version=None,
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                    ev = await repo.append_event(
                        session_id=session_id,
                        kind="approval" if kind == "tool_approval" else "interaction",
                        event_type="interaction/opened",
                        payload={
                            "interaction_id": iid,
                            "kind": kind,
                            "token": token,
                            "subject": prompt.subject,
                            "prompt": prompt.model_dump(),
                        },
                    )
                opened = {"interaction_id": iid, "token": token, "event": ev}
        await self.runner.publisher.publish(
            self.runner.publisher.session_channel(session_id), ev
        )
        return opened

    async def _wait_answer(
        self,
        request_id: str,
        timeout: float = 3600,  # noqa: ASYNC109
        *,
        thread_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """等网页上的答复。等不到按「停下」处理，不按拒绝（approval.md「问人怎么问」第 5 条）。
        委托被取消时这一问被放掉：回 (None, "cancelled")，按「停下」收尾。"""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending_approvals[request_id] = fut
        if thread_id:
            self.pending_threads[request_id] = thread_id
        try:
            answer = await asyncio.wait_for(fut, timeout=timeout)
            if request_id in self.released:
                return None, "cancelled"
            return answer, "user"
        except TimeoutError:
            return None, "timeout"
        finally:
            self.pending_approvals.pop(request_id, None)
            self.pending_threads.pop(request_id, None)
            self.released.discard(request_id)

    def release_waits(self, thread_id: str | None) -> int:
        """委托被取消：这条线上还在等人答的审批、提问一律放掉，让 Codex 那一轮马上收住，而不是卡到时限。"""
        if not thread_id:
            return 0
        n = 0
        for request_id, owner in list(self.pending_threads.items()):
            fut = self.pending_approvals.get(request_id)
            if owner == thread_id and fut is not None and not fut.done():
                self.released.add(request_id)
                fut.set_result(None)
                n += 1
        return n

    async def _settle_timeout(
        self, *, session: dict[str, Any], where: dict[str, Any], opened: dict[str, Any]
    ) -> None:
        """超时：把待办关掉、Task 从 WAITING 放回，再由停下的那一支收尾。"""
        async with self.runner.stores() as repo:
            try:
                await Ledger(repo).respond_interaction(
                    opened["interaction_id"],
                    token=opened["token"],
                    response={"decision": DECISION_CANCEL, "timeout": True},
                    owner_actor_id=str(session["owner_actor_id"]),
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "timeout settle failed interaction=%s", opened["interaction_id"]
                )

    async def _ask_human(
        self,
        rid: Any,
        *,
        request_id: str,
        method: str,
        params: dict[str, Any],
        summary: dict[str, Any],
        session: dict[str, Any],
        where: dict[str, Any],
        category: str,
        tokens: tuple[str, ...],
        after_decline: bool,
    ) -> None:
        client = self.client
        assert client is not None
        session_id = str(session["id"])
        wheel, task, pack, step = (
            where["wheel"],
            where["task"],
            where["pack"],
            where["step"],
        )
        task_id = str(task["id"]) if task else None
        step_id = step.step_id if step else None
        is_file = category.startswith("file_change")
        options = [
            {"id": DECISION_ACCEPT, "label": "允许一次"},
            {
                "id": DECISION_ACCEPT_TASK,
                "label": "这个任务里都允许" if task else "这段驾驶里都允许",
            },
            {"id": DECISION_DECLINE, "label": "拒绝这一步"},
            {"id": DECISION_CANCEL, "label": "停下"},
        ]
        subject: dict[str, Any] = {
            "request_id": request_id,
            **summary,
            "wheel": wheel,
            "after_decline": after_decline,
            "step": None
            if step is None
            else {
                "step_id": step.step_id,
                "title": step.title,
                "index": int(task["current_step"]) + 1,
            },
            "dependents": dependents(list(pack.workflow), step.step_id)
            if pack and step
            else [],
        }
        prompt = InteractionPrompt(
            title="改动需要你批准" if is_file else "命令需要你批准",
            question=params.get("reason") or "Codex 请求执行超出沙箱的动作",
            options=options,
            subject=subject,
        )
        opened = await self._open_ask(
            session=session,
            where=where,
            kind="tool_approval",
            prompt=prompt,
            waiting_reason=WaitingReason.APPROVAL,
        )
        response, source = await self._wait_answer(
            request_id, thread_id=thread_id_of(params)
        )
        if response is None:
            if source != "cancelled":
                await self._settle_timeout(session=session, where=where, opened=opened)
            response = {"decision": DECISION_CANCEL}
        decision = str(response.get("decision") or DECISION_DECLINE)
        if decision not in (
            DECISION_ACCEPT,
            DECISION_ACCEPT_TASK,
            DECISION_DECLINE,
            DECISION_CANCEL,
        ):
            decision = DECISION_DECLINE
        codex = (
            "accept"
            if decision in (DECISION_ACCEPT, DECISION_ACCEPT_TASK)
            else ("cancel" if decision == DECISION_CANCEL else "decline")
        )
        note = _note_of(response)
        async with self.runner.stores() as repo:
            async with repo.transaction():
                if decision == DECISION_ACCEPT_TASK:
                    await repo.insert_tool_grant(
                        session_id=session_id,
                        task_id=task_id,
                        category=category,
                        command_prefix=grant_prefix(category, tokens),
                        granted_by=str(session["owner_actor_id"]),
                    )
                await repo.log_approval(
                    session_id=session_id,
                    task_id=task_id,
                    request_id=request_id,
                    method=method,
                    summary={**summary, "note": note} if note else summary,
                    decision=codex,
                    source=source,
                    interaction_id=opened["interaction_id"],
                    wheel=wheel,
                    step_id=step_id,
                )
        thread_id = thread_id_of(params)
        if codex == "cancel" and thread_id:
            # 停下：整轮中断。顾问驾驶时这一步按「用户停下」收尾（advisor._step_once），不是出错
            self.user_stops[thread_id] = {
                "note": note,
                "source": source,
                "step_id": step_id,
            }
        await client.respond(rid, {"decision": codex})
        if codex == "decline" and note and thread_id:
            # 「拒绝，并告诉它怎么改」= decline + turn/steer（approval.md「改得不对」）
            asyncio.get_running_loop().create_task(
                self._steer(thread_id, note), name=f"steer:{thread_id}"
            )

    async def _steer(self, thread_id: str, text: str) -> None:
        try:
            await asyncio.sleep(0.2)
            client = await self.ensure_connected()
            await client.request(
                "turn/steer",
                {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
                timeout=30,
            )
        except Exception:  # noqa: BLE001
            log.exception("turn/steer failed thread=%s", thread_id)

    # ---- Codex 反过来问用户（request_user_input）：澄清，不是审批（approval.md）----
    async def _on_user_input(self, rid: Any, params: dict[str, Any]) -> None:
        client = self.client
        assert client is not None
        request_id = f"{self.sandbox_id}:{rid}"
        questions = [
            {
                "id": str(q.get("id") or f"q{i}"),
                "header": str(q.get("header") or ""),
                "question": str(q.get("question") or ""),
                "options": [
                    {
                        "label": str(o.get("label") or ""),
                        "description": str(o.get("description") or ""),
                    }
                    for o in (q.get("options") or [])
                    if isinstance(o, dict)
                ],
                "is_other": bool(q.get("isOther", q.get("is_other", False))),
                "is_secret": bool(q.get("isSecret", q.get("is_secret", False))),
            }
            for i, q in enumerate(params.get("questions") or [])
            if isinstance(q, dict)
        ]
        empty = {"answers": {q["id"]: {"answers": []} for q in questions}}
        async with self.runner.stores() as repo:
            session_id = await self.session_for_thread(thread_id_of(params), repo)
            if session_id is None or not questions:
                await client.respond(rid, empty)
                return
            session = await repo.get_session(session_id)
            where = await self._where(repo, session)
            wheel, task, pack, step = (
                where["wheel"],
                where["task"],
                where["pack"],
                where["step"],
            )
            task_id = str(task["id"]) if task else None
            step_id = step.step_id if step else None
            refuse = None
            if wheel == Wheel.advisor:
                if any(q["is_secret"] for q in questions):
                    refuse = "secret"  # 顾问不能向用户收口令
                elif pack is not None and step is not None:
                    policy = pack.policy_for(step)
                    if policy.questions == "deny":
                        refuse = "policy"
                    else:
                        asked = sum(
                            1
                            for it in await repo.list_interactions(
                                session_id=session_id, status=None
                            )
                            if it["kind"] == "question"
                            and str(it.get("task_id") or "") == (task_id or "")
                            and ((it.get("prompt") or {}).get("subject") or {}).get(
                                "step_id"
                            )
                            == step_id
                        )
                        if asked >= policy.max_questions:
                            refuse = "quota"
            if refuse:
                async with repo.transaction():
                    ev = await repo.append_event(
                        session_id=session_id,
                        kind="interaction",
                        event_type="question/refused",
                        payload={
                            "reason": refuse,
                            "questions": [
                                {k: v for k, v in q.items() if k != "options"}
                                for q in questions
                            ],
                            "step_id": step_id,
                        },
                        task_id=task_id,
                    )
                await self.runner.publisher.publish(
                    self.runner.publisher.session_channel(session_id), ev
                )
                await client.respond(rid, empty)
                return
        prompt = InteractionPrompt(
            title="Codex 想问你",
            question=questions[0]["question"]
            if len(questions) == 1
            else f"{len(questions)} 个问题",
            options=[],
            subject={
                "request_id": request_id,
                "questions": questions,
                "wheel": wheel,
                "step_id": step_id,
                "step": None
                if step is None
                else {
                    "step_id": step.step_id,
                    "title": step.title,
                    "index": int(task["current_step"]) + 1,
                },
            },
        )
        opened = await self._open_ask(
            session=session,
            where=where,
            kind="question",
            prompt=prompt,
            waiting_reason=WaitingReason.INPUT,
        )
        thread_id = thread_id_of(params)
        response, source = await self._wait_answer(request_id, thread_id=thread_id)
        if response is None:
            if source != "cancelled":
                await self._settle_timeout(session=session, where=where, opened=opened)
            if thread_id:
                self.user_stops[thread_id] = {
                    "note": None,
                    "source": "timeout",
                    "step_id": step_id,
                }
            await client.respond(rid, empty)
            if thread_id:
                try:
                    await self.interrupt(thread_id)
                except Exception:  # noqa: BLE001
                    log.exception("interrupt after question timeout failed")
            return
        answers_in = (
            response.get("answers")
            or (response.get("answer") or {}).get("answers")
            or {}
        )
        answers = {
            q["id"]: {
                "answers": [str(answers_in.get(q["id"]))]
                if answers_in.get(q["id"]) not in (None, "")
                else []
            }
            for q in questions
        }
        # 秘密答案只透传：不落账
        async with self.runner.stores() as repo:
            async with repo.transaction():
                ev = await repo.append_event(
                    session_id=str(session["id"]),
                    kind="interaction",
                    event_type="question/answered",
                    payload={
                        "interaction_id": opened["interaction_id"],
                        "step_id": step_id,
                        "answers": {
                            q["id"]: (
                                "[secret]"
                                if q["is_secret"]
                                else answers[q["id"]]["answers"]
                            )
                            for q in questions
                        },
                    },
                    task_id=task_id,
                )
        await self.runner.publisher.publish(
            self.runner.publisher.session_channel(str(session["id"])), ev
        )
        await client.respond(rid, {"answers": answers})

    def resolve_approval(self, request_id: str, response: dict[str, Any] | str) -> bool:
        fut = self.pending_approvals.get(request_id)
        if fut is None or fut.done():
            return False
        if isinstance(response, str):
            response = {"decision": response}
        fut.set_result(response)
        return True


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _note_of(response: dict[str, Any]) -> str | None:
    """用户附在答复上的话（「拒绝，并告诉它怎么改」「停下」可附）。"""
    answer = response.get("answer")
    if isinstance(answer, dict):
        text = answer.get("text") or answer.get("note")
        return str(text).strip() or None if text else None
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    return None


class Runner:
    def __init__(
        self,
        stores: WorkbenchStores,
        *,
        connector: AppServerConnector,
        publisher: EventPublisher,
        runner_id: str,
        environment_key: str = "user-pc",
        poll_seconds: float = 1.0,
        records: bool = False,
        prices: PriceList | None = None,
        deliverables: DeliverableCheck | None = None,
        data: KnowbotDataSource | None = None,
    ):
        self.records = records
        self.deliverables = deliverables
        self.data = data
        self.prices = prices or DEFAULT_PRICES
        self.stores = stores
        self.connector = connector
        self.publisher = publisher
        self.runner_id = runner_id
        self.environment_key = environment_key
        self.poll_seconds = poll_seconds
        self.links: dict[str, SandboxLink] = {}
        self.driving: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self.handled = 0
        self.lease_ttl_seconds = max(10, int(poll_seconds * 30))
        self.leased: set[str] = set()

    async def link_for(self, sandbox_id: str, repo: WorkbenchStore) -> SandboxLink:
        link = self.links.get(sandbox_id)
        if link is None:
            link = SandboxLink(self, await repo.get_sandbox(sandbox_id))
            self.links[sandbox_id] = link
        elif link.client is None or not link.client.connected:
            # 要重连时重读沙箱行：回收后重新拉起会换能力令牌（和地址），缓存的旧令牌会一直被 401
            # （KIND 2026-09-26：回收 → 拉起后会话里每条命令都 "HTTP 401"，重启 runner 才好）
            link.sandbox = await repo.get_sandbox(sandbox_id)
        return link

    async def run_forever(self) -> None:
        log.info("workbench runner starting id=%s", self.runner_id)
        while not self._stop.is_set():
            try:
                n = await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("runner iteration failed")
                n = 0
            if n == 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        for link in self.links.values():
            if link.client:
                await link.client.close()
        async with self.stores() as repo:
            async with repo.transaction():
                await repo.release_leases(runner_id=self.runner_id)

    def stop(self) -> None:
        self._stop.set()

    async def _drop_link(self, sandbox_id: str, why: str) -> None:
        link = self.links.pop(sandbox_id, None)
        if link is None:
            return
        log.warning("dropping sandbox link sandbox=%s: %s", sandbox_id, why)
        for fut in list(link.pending_approvals.values()):
            if not fut.done():
                fut.set_result({"decision": "decline"})
        if link.client:
            try:
                await link.client.close()
            except Exception:  # noqa: BLE001
                pass

    async def _take_over(self, repo: WorkbenchStore, sandbox_id: str) -> None:
        """刚拿到一个沙箱的租约（首次或接管）：作废上一任留下的未决工具审批，把没有驾驶者的 Task 重新排队。"""
        expired = await repo.expire_pending_tool_approvals(
            sandbox_id=sandbox_id, reason="runner restarted; Codex will ask again"
        )
        for it in expired:
            ev = await repo.append_event(
                session_id=it["session_id"],
                kind="interaction",
                event_type="interaction/expired",
                payload={
                    "interaction_id": it["id"],
                    "kind": "tool_approval",
                    "reason": "runner_restarted",
                },
                task_id=it["task_id"],
            )
            await self.publisher.publish(
                self.publisher.session_channel(it["session_id"]), ev
            )
        for task in await repo.active_tasks_for_sandbox(sandbox_id):
            if task["id"] in self.driving:
                continue
            await repo.enqueue_command(
                session_id=task["session_id"],
                sandbox_id=sandbox_id,
                kind="task.drive",
                payload={"task_id": task["id"], "resumed": True},
            )
        log.info("took over sandbox=%s expired_approvals=%d", sandbox_id, len(expired))

    async def run_once(self) -> int:
        async with self.stores() as repo:
            async with repo.transaction():
                await repo.requeue_stale_commands()
                kept = set(
                    await repo.renew_leases(
                        runner_id=self.runner_id, ttl_seconds=self.lease_ttl_seconds
                    )
                )
                fresh = await repo.acquire_leases(
                    runner_id=self.runner_id, ttl_seconds=self.lease_ttl_seconds
                )
                for sandbox_id in fresh:
                    await self._take_over(repo, sandbox_id)
                lost = self.leased - kept - set(fresh)
                self.leased = kept | set(fresh)
                commands = await repo.claim_commands(claimed_by=self.runner_id)
        for sandbox_id in lost:
            await self._drop_link(sandbox_id, "lease lost to another runner")
        for cmd in commands:
            error: str | None = None
            try:
                await self.handle(cmd)
            except Exception as exc:  # noqa: BLE001
                log.exception("command failed id=%s kind=%s", cmd["id"], cmd["kind"])
                error = f"{type(exc).__name__}: {exc}"[:2000]
            async with self.stores() as repo:
                async with repo.transaction():
                    await repo.finish_command(cmd["id"], error=error)
                    if error:
                        ev = await repo.append_event(
                            session_id=cmd["session_id"],
                            kind="error",
                            event_type="command/failed",
                            payload={
                                "command_id": cmd["id"],
                                "kind": cmd["kind"],
                                "error": error,
                            },
                        )
                        await self.publisher.publish(
                            self.publisher.session_channel(cmd["session_id"]), ev
                        )
            self.handled += 1
        return len(commands)

    # ---- 命令 ----
    async def handle(self, cmd: dict[str, Any]) -> None:
        kind, payload, session_id = cmd["kind"], cmd["payload"], cmd["session_id"]
        async with self.stores() as repo:
            session = await repo.get_session(session_id)
            link = await self.link_for(str(session["sandbox_id"]), repo)
        if kind == "task.reconnect":
            await self._reconnect_task(payload, session, link)
            return
        if kind == "task.drive":
            task_id = payload["task_id"]
            running = self.driving.get(task_id)
            if running and not running.done():
                return
            await self._thread_for_task(task_id, session, link)
            self.driving[task_id] = asyncio.create_task(
                self._drive(task_id, link), name=f"drive:{task_id}"
            )
            return
        if kind == "approval.respond":
            response = payload.get("response")
            if not isinstance(response, dict):
                response = {"decision": payload.get("decision")}
            ok = link.resolve_approval(payload["request_id"], response)
            if not ok:
                raise RuntimeError(
                    "no pending approval for this request (runner restarted or already answered)"
                )
            return
        client = await link.ensure_connected()
        if kind == "session.start_thread" or (
            kind == "turn.start" and session["thread_id"] is None
        ):
            await self._start_thread(link, client, session)
            if kind == "session.start_thread":
                return
            async with self.stores() as repo:
                session = await repo.get_session(session_id)
        if kind == "turn.start":
            session = await self._ensure_session_thread(link, client, session)
            if session["wheel"] != payload.get("by", "user"):
                raise RuntimeError(
                    f"wheel is held by {session['wheel']}; turn by {payload.get('by', 'user')} refused"
                )
            plan = await self.turn_plan(session=session, expert=False)
            await link.announce(session["thread_id"], plan)
            result = await client.request(
                "turn/start",
                {
                    "threadId": session["thread_id"],
                    "input": [{"type": "text", "text": payload["text"]}],
                    "clientUserMessageId": payload.get("request_id"),
                    **plan.settings,
                },
                timeout=120,
            )
            async with self.stores() as repo:
                async with repo.transaction():
                    ev = await repo.append_event(
                        session_id=session_id,
                        kind="turn",
                        event_type="turn/accepted",
                        payload={
                            "request_id": payload.get("request_id"),
                            "by": payload.get("by", "user"),
                            "turn": (result or {}).get("turn"),
                        },
                    )
                await self.publisher.publish(
                    self.publisher.session_channel(session_id), ev
                )
            return
        if kind == "turn.interrupt":
            # 用户点了停止。页面不用知道是哪一轮：停的是这条线正在跑的那一轮。
            # 取消委托时那一轮可能正卡在等人批准、等人回答：先把这几问放掉（按停下答 Codex），
            # 不然 Codex 一直等，要到 15 分钟时限才停（2026-10-14 所有者：专家页的停止不管用）
            released = (
                link.release_waits(session["thread_id"])
                if payload.get("task_id")
                else 0
            )
            try:
                stopped = await link.interrupt(
                    session["thread_id"], payload.get("turn_id")
                )
            except Exception:
                # 放掉之后 Codex 可能已经自己收住了这一轮，再中断会报「没有这一轮」
                if not released:
                    raise
                stopped = None
            async with self.stores() as repo:
                async with repo.transaction():
                    ev = await repo.append_event(
                        session_id=session_id,
                        kind="turn",
                        event_type="turn/stop_requested",
                        payload={
                            "turn_id": stopped,
                            "by": payload.get("by", "user"),
                            "running": stopped is not None,
                        },
                    )
                await self.publisher.publish(
                    self.publisher.session_channel(session_id), ev
                )
            return
        raise RuntimeError(f"unknown command kind {kind}")

    async def turn_plan(
        self,
        *,
        session: dict[str, Any] | None = None,
        thread_id: str | None = None,
        expert: bool,
    ) -> TurnPlan:
        """这一轮的执行环境、目录、沙箱策略、审批。每一轮都带全，不靠上一轮留下的。

        同一条线上可以先聊天、再工作、再交给专家；是什么由账里的种类与方向盘定，
        不由页面定（F-PROJ-03）。查不到这条线属于哪段对话时什么都不带，沿用线上原有的。
        """
        async with self.stores() as repo:
            if session is None and thread_id is not None:
                session = await repo.get_session_by_thread(thread_id)
            if session is None:
                return TurnPlan()
            prefs = await repo.get_prefs(str(session["owner_actor_id"]))
            online = True
            if session.get("environment_id") is not None:
                env = await repo.get_environment(str(session["environment_id"]))
                online = env.get("status") == "online"
        legacy = dict(session.get("thread_settings") or {})
        directory = session.get("project_root")
        facts: dict[str, Any] = {
            "directory": directory,
            "environment_key": (
                legacy.get("environmentId", self.environment_key) if directory else None
            ),
            "environment_online": online,
            "expert": expert,
        }
        kind = session.get("kind") or "work"
        async with self.stores() as repo:
            remembered = memory_note(
                await repo.list_memories(
                    str(session["owner_actor_id"]), enabled_only=True
                )
            )
        return TurnPlan(
            settings=turn_settings(
                kind,
                approval=legacy.get("approvalPolicy") or prefs.get("approval_policy"),
                **facts,
            ),
            mode=str(mode_of(kind, **facts)),
            directory=directory,
            session_id=str(session["id"]),
            facts=remembered,
        )

    async def _reconnect_task(
        self, payload: dict[str, Any], session: dict[str, Any], link: SandboxLink
    ) -> None:
        """在线心跳不等于执行环境就绪；实际问固定 app-server，再让账房恢复。"""
        task_id = payload["task_id"]
        environment_id = str(session.get("environment_id"))
        if payload.get("environment_id") != environment_id:
            return
        async with self.stores() as repo:
            task = await repo.get_task(
                task_id, owner_actor_id=str(session["owner_actor_id"])
            )
            environment = await repo.get_environment(environment_id)
            if (
                str(task["session_id"]) != str(session["id"])
                or task["state"] != TaskState.WAITING
                or task.get("waiting_reason") != WaitingReason.ENVIRONMENT
                or task.get("cancel_requested_at") is not None
                or task.get("active_attempt_id") is not None
                or task.get("active_interaction_id") is not None
                or environment.get("status") != "online"
            ):
                return
        env_key = (session.get("thread_settings") or {}).get(
            "environmentId", self.environment_key
        )
        try:
            client = await link.ensure_connected()
            # environment/knowfeed 会按 Codex 的恢复机制连接，不执行用户命令、不调模型。
            await client.request(
                "environment/info", {"environmentId": env_key}, timeout=10
            )
            status = await client.request(
                "environment/status", {"environmentId": env_key}, timeout=5
            )
        except (AppServerError, ConnectionError, TimeoutError):
            # 下次机器对账再探测；Task 保持 WAITING，避免制造失败的模型重试。
            return
        if not isinstance(status, dict) or status.get("status") != "ready":
            return
        async with self.stores() as repo:
            resumed = await Ledger(repo).resume_environment(
                task_id, session_id=str(session["id"]), environment_id=environment_id
            )
        if resumed:
            log.info("execution environment recovered task=%s", task_id)

    async def _thread_for_task(
        self, task_id: str, session: dict[str, Any], link: SandboxLink
    ) -> None:
        """从专家入口请的专家，对话是新建的，还没有线：先起线，再把线记到委托上。"""
        if session["thread_id"] is None:
            client = await link.ensure_connected()
            await self._start_thread(link, client, session)
        async with self.stores() as repo:
            task = await repo.get_task(task_id)
            if task["thread_id"]:
                return
            session = await repo.get_session(str(session["id"]))
            async with repo.transaction():
                await repo.cas_task(
                    task_id,
                    expected_version=int(task["state_version"]),
                    thread_id=session["thread_id"],
                )

    async def _drive(self, task_id: str, link: SandboxLink) -> None:
        advisor = Advisor(
            self.stores,
            link,
            records=self.records,
            deliverables=self.deliverables,
            data=self.data,
        )
        try:
            state = await advisor.drive(task_id)
            log.info("advisor stopped task=%s state=%s", task_id, state)
        except Exception as exc:  # noqa: BLE001
            log.exception("advisor crashed task=%s", task_id)
            try:
                await advisor.fail_after_crash(
                    task_id, f"platform error: {type(exc).__name__}"
                )
            except Exception:  # noqa: BLE001
                log.exception("could not close the crashed task=%s", task_id)
        finally:
            self.driving.pop(task_id, None)

    async def wait_drivers(self, timeout: float = 60) -> None:  # noqa: ASYNC109
        """测试与优雅停机用：等正在驾驶的顾问停下。"""
        tasks = list(self.driving.values())
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def _ensure_session_thread(
        self, link: SandboxLink, client: AppServerConnection, session: dict[str, Any]
    ) -> dict[str, Any]:
        """发 turn 前保证会话的线程在对面装载着；rollout 丢了就另起一条并换掉会话记的线程号。"""
        thread_id = session["thread_id"]
        try:
            await link.ensure_thread(thread_id)
            return session
        except AppServerError as exc:
            if not thread_gone(exc):
                raise
        log.warning(
            "thread %s gone on sandbox %s; starting a new one",
            thread_id,
            link.sandbox_id,
        )
        await self._start_thread(link, client, session, replaced=thread_id)
        async with self.stores() as repo:
            return await repo.get_session(str(session["id"]))

    async def _start_thread(
        self,
        link: SandboxLink,
        client: AppServerConnection,
        session: dict[str, Any],
        *,
        replaced: str | None = None,
    ) -> None:
        settings = dict(session.get("thread_settings") or {})
        env_key = settings.pop("environmentId", self.environment_key)
        if settings:  # 旧入口建的会话：沿用它记下的设置
            top = orchestrator_cwd(session["project_root"])
            params = {
                **({"cwd": top} if top else {}),
                "environments": [
                    {"environmentId": env_key, "cwd": session["project_root"]}
                ],
                **settings,
            }
            params.setdefault("config", dict(THREAD_CONFIG))
        else:
            async with self.stores() as repo:
                prefs = await repo.get_prefs(str(session["owner_actor_id"]))
            params = thread_settings(
                session.get("kind") or "work",
                directory=session.get("project_root"),
                environment_key=env_key if session.get("project_root") else None,
                approval=prefs.get("approval_policy"),
                model=prefs.get("model"),
            )
        try:
            result = await client.request("thread/start", params, timeout=60)
        except AppServerError as exc:
            raise RuntimeError(f"thread/start rejected: {exc}") from exc
        thread_id = ((result or {}).get("thread") or {}).get("id")
        if not thread_id:
            raise RuntimeError(f"thread/start returned no thread id: {result}")
        link.threads[thread_id] = str(session["id"])
        link.live_threads.add(thread_id)
        payload: dict[str, Any] = {
            "thread_id": thread_id,
            "environment": env_key if session.get("project_root") else None,
            "model": link.remember_model(thread_id, result),
        }
        if replaced:
            payload["replaced_thread_id"] = replaced
        async with self.stores() as repo:
            async with repo.transaction():
                await repo.set_session_thread(str(session["id"]), thread_id)
                ev = await repo.append_event(
                    session_id=str(session["id"]),
                    kind="thread",
                    event_type="session/thread_started",
                    payload=payload,
                )
            await self.publisher.publish(
                self.publisher.session_channel(str(session["id"])), ev
            )
