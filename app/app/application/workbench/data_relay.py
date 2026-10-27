"""工作台 MCP 挂出 knowbase 数据 MCP 的读工具（SDD 0048，工具层中继）。

沙箱里的 Codex 只连工作台 MCP（沙箱配置由平台的 provisioner 落，只认工作台这一个地址，
不发 knowbase 的令牌——「用户的沙箱到不了平台的数」）。数据工具照 knowbase 数据 MCP 的
定义列出来，只把 `run` / `session` 换成工作台的 `task` / `conversation`：

- 带 `task`：先核这件委托是这个用户的、专家正在做（同读项目记录），换成 knowbase 认的
  运行授权 ref `knowbench:<幂等钥匙>`；
- 带 `conversation`：核这段对话是他的、在 knowbase 登记过会话，换成会话编号；
- `scope=market`、`lookup`、`describe_items` 不要运行授权（全市场公开原件由开关管）。

每次调用当场签一张几分钟有效的 `knowbase-data` 令牌（只许这一个工具），原样转给
knowbase；能读什么由 knowbase 每次按授权核，工作台拿不到用户没授权过的数据。
"""

from __future__ import annotations

import copy
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.records import ProjectRecords, as_id
from app.application.workbench.tokens import TokenIssuer
from app.domain.workbench.errors import RecordsRefused
from app.domain.workbench.knowbots import DATA_MCP_TOOLS

NO_SCOPE = frozenset({"describe_items", "lookup"})
# 中继的工具：专家步骤能用的那几个，加会话里列他名下资料的 list_library
RELAYED = DATA_MCP_TOOLS | {"list_library"}
_TASK = {
    "type": "string",
    "description": "The task id given in the step instructions.",
}
_CONVERSATION = {
    "type": "string",
    "description": "In a chat: the conversation id given in the instructions.",
}
_TOOLS_TTL_SECONDS = 600
_tools_cache: dict[str, Any] = {"at": 0.0, "tools": []}


class KnowbaseDataMcp(Protocol):
    async def data_mcp(self, token: str, message: dict[str, Any]) -> dict[str, Any]: ...


def run_ref(task: dict[str, Any]) -> str:
    """这件委托在 knowbase 那边的运行授权 ref（请专家时就这么签的）。"""
    return f"knowbench:{task['idempotency_key']}"[:255]


def rewrite(spec: dict[str, Any]) -> dict[str, Any]:
    """knowbase 的工具定义 → 工作台的：run/session 换成 task/conversation。"""
    out = copy.deepcopy(spec)
    schema = out.setdefault("inputSchema", {"type": "object", "properties": {}})
    props = schema.setdefault("properties", {})
    had_run = props.pop("run", None) is not None
    had_session = props.pop("session", None) is not None
    if had_run:
        props["task"] = _TASK
    if had_session:
        props["conversation"] = _CONVERSATION
    required = [r for r in schema.get("required") or [] if r != "session"]
    schema["required"] = ["task" if r == "run" else r for r in required]
    if not schema["required"]:
        schema.pop("required")
    return out


class DataRelay:
    def __init__(
        self,
        records: ProjectRecords,
        knowbase: KnowbaseDataMcp,
        issuer: TokenIssuer,
        *,
        sandbox: str,
    ) -> None:
        self.records = records
        self.repo: WorkbenchStore = records.repo
        self.knowbase = knowbase
        self.issuer = issuer
        self.sandbox = sandbox
        # compute 取事实编号用（同一个 knowbase、同一个签发器）
        from app.application.workbench.run_reader import RunReader

        self.reader = RunReader(knowbase, issuer)

    async def tools(self) -> list[dict[str, Any]]:
        """数据工具的定义（照 knowbase，缓存几分钟）；knowbase 不通就是空的。"""
        if time.monotonic() - _tools_cache["at"] < _TOOLS_TTL_SECONDS:
            return list(_tools_cache["tools"])
        try:
            subject = await self._subject()
            token = self.issuer.data_token(
                subject, sandbox=self.sandbox, tools=sorted(RELAYED)
            ).token
            reply = await self.knowbase.data_mcp(
                token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
        except Exception:  # noqa: BLE001 — 数据工具暂时不列，项目工具照常
            return []
        listed = (reply.get("result") or {}).get("tools") or []
        tools = [rewrite(t) for t in listed if t.get("name") in RELAYED]
        _tools_cache.update(at=time.monotonic(), tools=tools)
        return list(tools)

    async def call(self, mid: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """换好范围、签好令牌，转给 knowbase；回 knowbase 的 JSON-RPC 回复（编号换回来）。"""
        forwarded = {k: v for k, v in args.items() if k not in ("task", "conversation")}
        working = None
        if name not in NO_SCOPE and args.get("scope") != "market":
            if args.get("task"):
                working = await self.records._working(args["task"])
                forwarded["run"] = run_ref(working)
            elif args.get("conversation"):
                forwarded["session"] = await self._knowbase_session(
                    args["conversation"]
                )
            else:
                raise RecordsRefused(
                    "give the task id (or, in a chat, the conversation)"
                )
        token = self.issuer.data_token(
            await self._subject(), sandbox=self.sandbox, tools=[name]
        ).token
        reply = await self.knowbase.data_mcp(
            token,
            {
                "jsonrpc": "2.0",
                "id": mid,
                "method": "tools/call",
                "params": {"name": name, "arguments": forwarded},
            },
        )
        if working is not None:
            await self.records._noted(working, name)
        return {**reply, "id": mid}

    async def _subject(self) -> str:
        subject = await self.repo.subject_of_actor(self.records.owner)
        if subject is None:
            raise RecordsRefused("this account is not known to the data service")
        return subject

    async def _knowbase_session(self, conversation: Any) -> str:
        session = await self.repo.get_session(
            as_id(conversation, "conversation"), owner_actor_id=self.records.owner
        )
        found = session.get("knowbase_session_id")
        expires = session.get("knowbase_session_expires_at")
        if not found or (expires is not None and expires <= datetime.now(UTC)):
            raise RecordsRefused(
                "this chat has no data session yet; ask the user to send a message"
            )
        return str(found)
