"""工作台替调用者绑定 KnowBase 资料范围。

聊天和工作以 conversation 解析当前账号的资料会话，专家以 task 解析本次委托的
运行授权。scope=market 由 KnowBase 核市场资格。run 和 session 只由宿主填写。
数据函数从管理中心发现，用绑定本次调用、版本和参数的票据执行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from app.application.ports.workbench import WorkbenchStore
from app.application.workbench.records import ProjectRecords, as_id
from app.application.workbench.tokens import TokenIssuer
from app.domain.workbench.errors import RecordsRefused, UserDataUnavailable

NO_SCOPE = frozenset({"describe_items", "lookup"})


class KnowbaseDataClient(Protocol):
    async def invoke_data_query(self, name: str, token: str, body: dict) -> dict: ...


async def run_ref(repo: WorkbenchStore, task: dict[str, Any]) -> str:
    """这件委托在 knowbase 那边的运行授权 ref：请专家的是服务端生成、记在清单那一行的；
    knowlab 测试的用例委托是 knowlab 签测试授权用的 `<run_id>:<case_id>`（带测试集的
    用例没有清单那一行，按用例查）。都没有的（内置专家）没有运行授权，读不了用户的数据。"""
    row = await repo.get_task_resolution(str(task["id"]))
    ref = (row or {}).get("grant_ref") or await repo.test_case_ref(str(task["id"]))
    if not ref:
        raise RecordsRefused("this task has no data of the user to read")
    return str(ref)


class ScopedDataFunctions:
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
        return getattr(self.knowbase, "function_configured", True) is not False

    async def data_query(self, revision, args, call_id):
        from app.application.workbench.data_function import DataFunction

        if not self.available:
            raise UserDataUnavailable("The data service is unavailable")
        if (args.get("task") and args.get("conversation")) or (
            args.get("scope") == "market" and ("task" in args or "conversation" in args)
        ):
            raise RecordsRefused("Use one data scope for this data query")
        working, forwarded = await forward_arguments(self.records, revision.name, args)
        if "scope" in revision.contract.input_schema["properties"]:
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

    async def _subject(self) -> str:
        subject = await self.repo.subject_of_actor(self.records.owner)
        if subject is None:
            raise RecordsRefused("this account is not known to the data service")
        return subject


async def forward_arguments(records: ProjectRecords, name, args):
    if "run" in args or "session" in args:
        raise RecordsRefused("data service grants are assigned by the workbench")
    forwarded = {k: v for k, v in args.items() if k not in ("task", "conversation")}
    working = None
    if name not in NO_SCOPE and args.get("scope") != "market":
        if args.get("task"):
            working = await records._working(args["task"])
            forwarded["run"] = await run_ref(records.repo, working)
        elif args.get("conversation"):
            forwarded["session"] = await _knowbase_session(
                records, args["conversation"]
            )
        else:
            raise RecordsRefused("give the task id (or, in a chat, the conversation)")
    return working, forwarded


async def _knowbase_session(records: ProjectRecords, conversation: Any) -> str:
    session = await records.repo.get_session(
        as_id(conversation, "conversation"), owner_actor_id=records.owner
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
