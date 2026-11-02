"""工作台 MCP 挂出 knowbase 数据 MCP 的读工具（SDD 0048，工具层中继）。

沙箱里的 Codex 只连工作台 MCP（沙箱配置由平台的 provisioner 落，只认工作台这一个地址，
不发 knowbase 的令牌——「用户的沙箱到不了平台的数」）。数据工具照 knowbase 数据 MCP 的
定义列出来，只把 `run` / `session` 换成工作台的 `task` / `conversation`：

- 带 `task`：先核这件委托是这个用户的、专家正在做（同读项目记录），换成 knowbase 认的
  运行授权 ref（请专家时服务端生成、记在清单那一行的 `grant_ref`）；
- 带 `conversation`：核这段对话是他的、在 knowbase 登记过会话、眼下不是专家在做（对话的
  方向盘在用户手里），换成会话编号。委托步骤只认 `task`：工作台 MCP 的令牌只绑用户与沙箱，
  分不出调用的是对话还是委托步骤，所以按对话这一头认——专家正在做的那段对话拒收
  `conversation`，免得委托绕开这件委托的授权去读会话范围（独立审 10-30 第二轮）；
- `scope=market`、`lookup`、`describe_items` 不要运行授权（全市场公开原件由开关管）。

每次调用当场签一张几分钟有效的 `knowbase-data` 令牌（只许这一个工具），原样转给
knowbase；能读什么由 knowbase 每次按授权核，工作台拿不到用户没授权过的数据。
"""

from __future__ import annotations

import copy
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.records import ProjectRecords, as_id
from app.application.workbench.tokens import TokenIssuer
from app.domain.workbench.errors import RecordsRefused, UserDataUnavailable
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
_tools_cache: OrderedDict[tuple, tuple[float, list[dict[str, Any]]]] = OrderedDict()
log = logging.getLogger(__name__)


class KnowbaseDataMcp(Protocol):
    async def data_mcp(self, token: str, message: dict[str, Any]) -> dict[str, Any]: ...


async def run_ref(repo: WorkbenchStore, task: dict[str, Any]) -> str:
    """这件委托在 knowbase 那边的运行授权 ref：请专家的是服务端生成、记在清单那一行的；
    knowlab 测试的用例委托是 knowlab 签测试授权用的 `<run_id>:<case_id>`（带测试集的
    用例没有清单那一行，按用例查）。都没有的（内置专家）没有运行授权，读不了用户的数据。"""
    row = await repo.get_task_resolution(str(task["id"]))
    ref = (row or {}).get("grant_ref") or await repo.test_case_ref(str(task["id"]))
    if not ref:
        raise RecordsRefused("this task has no data of the user to read")
    return str(ref)


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
        """只缓存完整的成功目录；配置了数据服务却发现失败时明确报错。"""
        # Deliberately unconfigured installations retain project-only tools.
        # A configured but broken service must never advertise a partial catalog.
        if getattr(self.knowbase, "data_mcp_configured", True) is False:
            return []
        try:
            subject = await self._subject()
            key = (
                self.knowbase,
                subject,
                self.sandbox,
                self.issuer.kid,
                self.issuer.issuer,
            )
            cached = _tools_cache.get(key)
            if cached and time.monotonic() - cached[0] < _TOOLS_TTL_SECONDS:
                _tools_cache.move_to_end(key)
                return copy.deepcopy(cached[1])
            token = self.issuer.data_token(
                subject, sandbox=self.sandbox, tools=sorted(RELAYED)
            ).token
            reply = await self.knowbase.data_mcp(
                token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            result = reply.get("result")
            listed = result.get("tools") if isinstance(result, dict) else None
            if reply.get("error") or not isinstance(listed, list):
                raise ValueError("invalid tool catalog")
            specs = {}
            for spec in listed:
                if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
                    raise ValueError("invalid tool definition")
                name = spec["name"]
                if name not in RELAYED:
                    continue
                if name in specs or not isinstance(spec.get("inputSchema"), dict):
                    raise ValueError("invalid or duplicate tool definition")
                specs[name] = rewrite(spec)
            if set(specs) != RELAYED:
                raise ValueError("incomplete tool catalog")
            tools = list(specs.values())
        except Exception as exc:
            # Never log upstream bodies or tokens. HTTP/protocol codes are enough
            # to distinguish unavailable transport from missing user materials.
            log.warning(
                "workbench_data_tools_unavailable cause=%s code=%s",
                type(exc).__name__,
                getattr(exc, "code", "catalog_invalid"),
            )
            raise UserDataUnavailable(
                "The data tool catalog is unavailable. Retry tools/list after the "
                "service recovers; this does not mean the task has no data."
            ) from None
        _tools_cache[key] = (time.monotonic(), copy.deepcopy(tools))
        while len(_tools_cache) > 128:
            _tools_cache.popitem(last=False)
        return tools

    async def call(self, mid: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """换好范围、签好令牌，转给 knowbase；回 knowbase 的 JSON-RPC 回复（编号换回来）。"""
        if "run" in args or "session" in args:
            raise RecordsRefused("data service grants are assigned by the workbench")
        forwarded = {k: v for k, v in args.items() if k not in ("task", "conversation")}
        working = None
        if name not in NO_SCOPE and args.get("scope") != "market":
            if args.get("task"):
                working = await self.records._working(args["task"])
                forwarded["run"] = await run_ref(self.repo, working)
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
        if session.get("wheel") != "user" or session.get("active_task_id"):
            # 专家正在这段对话里做委托：读数据只认 task（这件委托的授权）
            raise RecordsRefused("in an expert's step, give the task id, not the chat")
        found = session.get("knowbase_session_id")
        expires = session.get("knowbase_session_expires_at")
        if not found or (expires is not None and expires <= datetime.now(UTC)):
            raise RecordsRefused(
                "this chat has no data session yet; ask the user to send a message"
            )
        return str(found)
