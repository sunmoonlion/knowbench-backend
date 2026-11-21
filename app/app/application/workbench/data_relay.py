"""工作台替调用者绑定 KnowBase 资料范围。

聊天和工作以 conversation 解析当前账号的资料会话，专家以 task 解析本次委托的
运行授权。scope=market 由 KnowBase 核市场资格。run 和 session 只由宿主填写。
已登记的数据函数使用绑定调用 ID、版本和参数的签名票据直接派发；其余中继读工具
仍按操作签发数据 MCP 令牌。两条路径均在 KnowBase 执行时核对资料授权。
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
from app.domain.workbench.data_function import DATA_QUERIES
from app.domain.workbench.errors import RecordsRefused, UserDataUnavailable
from app.domain.workbench.knowbots import DATA_MCP_TOOLS
from app.domain.workbench.native_functions import workbench_data_descriptor

NO_SCOPE = frozenset({"describe_items", "lookup"})
# 中继的工具：专家步骤能用的那几个，加会话里列他名下资料的 list_library
RELAYED = (DATA_MCP_TOOLS | {"list_library"}) - DATA_QUERIES
_TOOLS_TTL_SECONDS = 600
_tools_cache: OrderedDict[tuple, tuple[float, list[dict[str, Any]]]] = OrderedDict()
log = logging.getLogger(__name__)


class KnowbaseDataClient(Protocol):
    async def invoke_data_query(self, name: str, token: str, body: dict) -> dict: ...

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


class DataRelay:
    def __init__(
        self,
        records: ProjectRecords,
        knowbase: KnowbaseDataClient,
        issuer: TokenIssuer,
        *,
        sandbox: str,
    ) -> None:
        self.records = records
        self.repo: WorkbenchStore = records.repo
        self.knowbase = knowbase
        self.issuer = issuer
        self.sandbox = sandbox

    def reader_for_call(self, parent_call_id):
        from app.application.workbench.run_reader import RunReader

        return RunReader(self.knowbase, self.issuer, parent_call_id=parent_call_id)

    @property
    def available(self):
        return getattr(self.knowbase, "data_mcp_configured", True) is not False

    async def tools(self) -> list[dict[str, Any]]:
        """只缓存完整的成功目录；配置了数据服务却发现失败时明确报错。"""
        # Deliberately unconfigured installations retain project-only tools.
        # A configured but broken service must never advertise a partial catalog.
        if not self.available:
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
                specs[name] = workbench_data_descriptor(spec)
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
        if name not in RELAYED:
            raise RecordsRefused("This operation has no data MCP binding")
        working, forwarded = await self.forward_arguments(name, args)
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

    async def data_query(self, revision, args, call_id):
        from app.application.workbench.data_function import DataFunction

        if not self.available:
            raise UserDataUnavailable("The data service is unavailable")
        if (args.get("task") and args.get("conversation")) or (
            args.get("scope") == "market" and ("task" in args or "conversation" in args)
        ):
            raise RecordsRefused("Use one data scope for this data query")
        working, forwarded = await self.forward_arguments(revision.name, args)
        forwarded.setdefault("scope", "run")
        receipt = await DataFunction(self.knowbase, self.issuer).invoke(
            revision,
            subject=await self._subject(),
            sandbox=self.sandbox,
            call_id=call_id,
            arguments=forwarded,
        )
        if working is not None:
            await self.records._noted(working, revision.name, call_id=call_id)
        return receipt["output"]

    async def forward_arguments(self, name, args):
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
        return working, forwarded

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
