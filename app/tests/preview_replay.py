"""重放磁带的假对面：用户的轮次重放真的 Codex 录下来的消息，专家的轮次按样例交回。

磁带在 tests/preview_tapes/，由 tests/drivers/workbench_tape_driver.py 录。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from usage_support import MODEL, UsageMeter
from websockets.asyncio.server import serve

HANG = object()  # 专家的这一步不交回：委托停在「正在做」
STEP = re.compile(r"^# Advisor step: .*\((?P<id>[a-z_]+), v")


class Plan:
    """一条线上对面怎么答。tape：用户每一轮的磁带；expert：专家每一步的交回物。"""

    def __init__(
        self,
        *,
        tape: list[dict[str, Any]] | None = None,
        expert: list[Any] | None = None,
        tools: dict[str, list[dict[str, Any]]] | None = None,
        tokens: int = 5000,
    ) -> None:
        self.tape = list(tape or [])
        self.expert = list(expert or [])
        self.tools = tools or {}
        self.tokens = tokens


class ReplayAppServer:
    """假的 app-server。用户的轮次重放磁带（真的 Codex 录下来的）；专家的轮次按样例交回。"""

    def __init__(self) -> None:
        self.plans: list[Plan] = []
        self.threads: dict[str, Plan] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.turn_inputs: list[str] = []
        self.meter = UsageMeter()
        self.open_turns: dict[str, Any] = {}  # 一直开着的轮次：等着被停下
        self.interrupted: list[str | None] = []
        self.last_usage: dict[str, dict[str, Any]] = {}  # 每条线最近报的一次用量
        self.replayed = 0
        self.port = 0
        self.server: Any = None

    def next_thread(self, plan: Plan) -> None:
        self.plans.append(plan)

    async def start(self) -> None:
        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                if "method" in msg and "id" in msg:
                    asyncio.create_task(self.handle(ws, msg))
                elif "id" in msg:
                    waiting = self.pending.pop(str(msg["id"]), None)
                    if waiting and not waiting.done():
                        waiting.set_result(msg.get("result"))

        self.server = await serve(handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        for waiting in self.pending.values():
            waiting.cancel()
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, ws, msg) -> None:
        method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]

        async def send(body: dict[str, Any]) -> None:
            await ws.send(json.dumps(body, ensure_ascii=False))

        if method == "thread/start":
            thread = f"thread-{uuid.uuid4().hex[:12]}"
            self.threads[thread] = self.plans.pop(0) if self.plans else Plan()
            await send(
                {"id": rid, "result": {"thread": {"id": thread}, "model": MODEL}}
            )
            return
        if method == "turn/interrupt":
            self.interrupted.append(params.get("turnId"))
            await send({"id": rid, "result": {}})
            stopped = self.open_turns.pop(str(params.get("turnId")), None)
            if stopped is not None:
                reply, thread = stopped
                await reply(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread,
                            "turn": {"id": params["turnId"], "status": "interrupted"},
                        },
                    }
                )
            return
        if method == "thread/read":
            thread = str(params["threadId"])
            active = any(t == thread for _, t in self.open_turns.values())
            await send(
                {
                    "id": rid,
                    "result": {
                        "thread": {
                            "id": thread,
                            "status": {"type": "active" if active else "idle"},
                        }
                    },
                }
            )
            return
        if method == "thread/unsubscribe":
            await send({"id": rid, "result": {"status": "unsubscribed"}})
            return
        if method == "thread/resume":
            await send(
                {
                    "id": rid,
                    "result": {"model": MODEL, "thread": {"id": params["threadId"]}},
                }
            )
            # 真的 Codex 在重新装载一条线时，把上一次的用量原样再报一遍
            again = self.last_usage.get(str(params.get("threadId")))
            if again is not None:
                self.replayed += 1
                await send({"method": "thread/tokenUsage/updated", "params": again})
            return
        if method != "turn/start":
            await send({"id": rid, "result": {}})
            return
        thread = params["threadId"]
        plan = self.threads.setdefault(thread, Plan())
        turn = f"turn-{uuid.uuid4().hex[:12]}"
        text = params["input"][0]["text"]
        self.turn_inputs.append(text)
        await send({"id": rid, "result": {"turn": {"id": turn, "threadId": thread}}})
        await asyncio.sleep(0.01)
        step = STEP.match(text)
        if step:
            await self.expert_turn(send, plan, thread, turn, step.group("id"))
        else:
            await self.taped_turn(send, plan, thread, turn)

    async def expert_turn(self, send, plan: Plan, thread: str, turn: str, step: str):
        def note(method: str, **params: Any) -> dict[str, Any]:
            body = {"threadId": thread, "turnId": turn, **params}
            if method == "thread/tokenUsage/updated":
                self.last_usage[thread] = body
            return {"method": method, "params": body}

        await send(note("turn/started", turn={"id": turn, "status": "inProgress"}))
        reply = plan.expert.pop(0) if plan.expert else {"echo": step}
        for n, call in enumerate(plan.tools.get(step, [])):
            await send(note("item/completed", item={"id": f"call-{turn}-{n}", **call}))
        if reply is HANG:
            # 模型调用了一次、动作还在做：报一次用量，然后这一轮就一直开着，直到被停下
            await send(
                note(
                    "thread/tokenUsage/updated",
                    tokenUsage=self.meter.report(thread, plan.tokens),
                )
            )
            self.open_turns[turn] = (send, thread)
            return
        said = (
            reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        )
        await send(
            note(
                "item/completed",
                item={"type": "agentMessage", "id": f"msg-{turn}", "text": said},
            )
        )
        await send(
            note(
                "thread/tokenUsage/updated",
                tokenUsage=self.meter.report(thread, plan.tokens),
            )
        )
        await send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread,
                    "turn": {"id": turn, "status": "completed"},
                },
            }
        )

    async def taped_turn(self, send, plan: Plan, thread: str, turn: str) -> None:
        if not plan.tape:
            await send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread,
                        "turn": {"id": turn, "status": "completed"},
                    },
                }
            )
            return
        recorded = plan.tape.pop(0)
        was_thread = was_turn = None
        for message in recorded["messages"]:
            params = message["params"]
            was_thread = was_thread or params.get("threadId")
            was_turn = (
                was_turn or params.get("turnId") or (params.get("turn") or {}).get("id")
            )
        for message in recorded["messages"]:
            body = json.dumps(message["params"], ensure_ascii=False)
            if was_thread:
                body = body.replace(str(was_thread), thread)
            if was_turn:
                body = body.replace(str(was_turn), turn)
            params = json.loads(body)
            if message["kind"] == "request":
                asked = f"srv-{uuid.uuid4().hex[:8]}"
                answered: asyncio.Future = asyncio.get_running_loop().create_future()
                self.pending[asked] = answered
                await send({"id": asked, "method": message["method"], "params": params})
                self.open_turns[turn] = (send, thread)
                try:
                    await answered  # 用户没答之前，这一轮就停在这里
                except asyncio.CancelledError:
                    return
                self.open_turns.pop(turn, None)
                continue
            if message["method"] == "thread/tokenUsage/updated":
                self.last_usage[thread] = params
            await send({"method": message["method"], "params": params})
            await asyncio.sleep(0.002)
