"""工作台向 Codex 暴露的 Streamable HTTP 工具入口。

资料操作从方法技能管理中心查可见版本。按聊天、工作或当前专家步骤核权后，
工作台将会话或委托换成资料授权引用，签发只覆盖本次调用的票据，交 KnowBase 执行。
七项工作台操作同样按已登记版本执行，在本委托范围调用各自的处理函数。
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench.calls import CallLedger
from app.application.workbench.expert_data import find_subject, request_data
from app.application.workbench.knowbot_compute import KnowbotCompute
from app.application.workbench.method_tools import MethodToolGuard
from app.application.workbench.records import ProjectRecords
from app.application.workbench.scoped_data_functions import ScopedDataFunctions
from app.application.workbench.tokens import TokenIssuer
from app.application.workbench.tool_registry import registered_functions
from app.bootstrap.workbench import (
    knowbase_v2,
    knowfeed_supply,
    workbench_store,
)
from app.domain.workbench.capabilities import CapabilityError
from app.domain.workbench.data_function import DATA_QUERIES
from app.domain.workbench.errors import WorkbenchError
from app.domain.workbench.native_functions import (
    BINDINGS,
    COMPUTE,
    PROJECT_LIST,
    PROJECT_READ,
)
from app.domain.workbench.projects import TOOL_RESULT_MAX_BYTES
from app.domain.workbench.tool_registry import ToolRegistry
from app.domain.workbench.tool_schema import schema_validator
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.workbench_routes import (
    require_workbench_enabled,
    token_issuer,
)
from core.config import get_settings

log = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_KNOWFEED = {"name": "sunmoon-workbench", "version": "0.1.0"}


class RateLimiter:
    def __init__(self) -> None:
        self.calls: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, per_minute: int) -> bool:
        now = time.monotonic()
        q = self.calls[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            return False
        q.append(now)
        return True


limiter = RateLimiter()


def records_enabled() -> bool:
    """沙箱那头配好这个服务之前是关着的：关着时这个地址不存在。测试用依赖覆盖。"""
    return get_settings().workbench_records_mcp_enabled


def records_rate() -> int:
    return get_settings().workbench_records_mcp_rate_per_minute


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(mid: Any, message: str) -> dict[str, Any]:
    return _ok(mid, {"content": [{"type": "text", "text": message}], "isError": True})


async def handle(
    records: ProjectRecords,
    message: dict[str, Any],
    *,
    owner: str,
    per_minute: int,
    relay: ScopedDataFunctions | None = None,
    guard: MethodToolGuard | None = None,
) -> dict[str, Any] | None:
    method = message.get("method")
    mid = message.get("id")
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _err(None, -32600, "invalid request")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _err(mid, -32602, "params must be an object")
    if method == "initialize":
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[0])
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _ok(
            mid,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_KNOWFEED,
                "instructions": (
                    "Use the registered tools available to this conversation or task. "
                    "For task operations copy the task id from the current step. "
                    "For conversation data use the current conversation id. "
                    "Discovering a supplier or proposing data does not authorize collection."
                ),
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        try:
            functions = await registered_functions(
                records.repo, data_available=bool(relay and relay.available)
            )
            tools = ToolRegistry([reg for _, reg in functions.values()]).descriptors()
        except WorkbenchError as exc:
            return _err(mid, -32003, exc.message)
        return _ok(mid, {"tools": tools})
    if method != "tools/call":
        return None if mid is None else _err(mid, -32601, f"method not found: {method}")
    name = str(params.get("name") or "")
    args = params.get("arguments", {})
    if name not in BINDINGS:
        return _err(mid, -32602, f"unknown tool: {name}")
    if not isinstance(args, dict):
        return _err(mid, -32602, "arguments must be an object")
    if not limiter.allow(owner, per_minute):
        return _tool_error(mid, "rate limit exceeded; retry later")
    if name == "compute" and (
        not isinstance(args.get("task"), str) or not args["task"].strip()
    ):
        return _tool_error(
            mid,
            "compute requires a non-empty task argument. Copy the task id from "
            "the current step instructions and send a JSON object with task plus "
            "name/expression/inputs, or task plus batch. An empty object performs "
            "no calculation; do not repeat it. This does not mean the task was deleted.",
        )
    if guard is None:
        return _tool_error(mid, "A verified runtime invocation is required")
    ledger = CallLedger(records.repo, owner)
    try:
        invocation, task_id = await guard.check(name, args, params.get("_meta"))
        functions = await registered_functions(
            records.repo,
            data_available=bool(relay and relay.available),
            name=name,
        )
        if name not in functions:
            raise CapabilityError("Function is not registered for native execution")
        function, registration = functions[name]
        receipt, fresh = await ledger.begin(
            invocation, registration, args, task_id=task_id, function=function
        )
        if not fresh:
            response = receipt["detail"].get("response")
            if response is None:
                return _tool_error(
                    mid,
                    "This call is already recorded and may have executed; it will not be repeated.",
                )
            return {"jsonrpc": "2.0", "id": mid, **response}
    except WorkbenchError as exc:
        log.info("workbench_mcp_refused tool=%s code=%s", name, exc.code)
        return _tool_error(mid, exc.message)
    response, status = await dispatch(
        records, mid, name, args, relay, function=function, receipt=receipt
    )
    if "result" in response:
        metadata = response["result"].get("_meta")
        if not isinstance(metadata, dict):
            metadata = {}
        response["result"]["_meta"] = {
            **metadata,
            "call_id": receipt["id"],
            "call_status": status,
        }
    await ledger.finish(receipt, response, status)
    return response


async def _execute_local(records, name, args, relay, receipt):
    if name == PROJECT_LIST:
        return await records.list_conversations(args["task"], call_id=receipt["id"])
    if name == PROJECT_READ:
        return await records.read_conversation(
            args["task"],
            args["conversation"],
            args.get("page", 1),
            call_id=receipt["id"],
        )
    if name == COMPUTE:
        reader = relay.reader_for_call(receipt["id"]) if relay else None
        return await KnowbotCompute(records, reader, call_id=receipt["id"]).execute(
            args
        )
    working = await records._working(args["task"])
    if name == "request_data":
        return await request_data(
            records.repo, knowbase_v2(), working, args["needs"], call_id=receipt["id"]
        )
    if name == "discover_source_tools":
        subject = await records.repo.subject_of_actor(str(working["owner_actor_id"]))
        if subject is None:
            return {"sources": []}
        return await knowfeed_supply().source_tools(subject, args.get("service"))
    if name == "find_subject":
        return await find_subject(knowfeed_supply(), args.get("name"))
    raise CapabilityError("Missing local function implementation")


async def dispatch(records, mid, name, args, relay, *, function, receipt):
    failure_status = "failed"
    try:
        current = (
            await registered_functions(
                records.repo,
                data_available=bool(relay and relay.available),
                name=name,
            )
        ).get(name)
        if (
            not receipt
            or not function
            or not current
            or current[0] != function
            or (
                receipt["detail"]["operation"].get("registered_source")
                != function.model_dump(exclude={"contract"})
            )
        ):
            return _tool_error(
                mid, "Registered function changed before dispatch"
            ), "failed"
        if function.provider == "knowbench":
            # These operations can create an artifact or request external work.
            if name not in (
                PROJECT_LIST,
                PROJECT_READ,
                "discover_source_tools",
                "find_subject",
            ):
                failure_status = "unknown"
            output = await _execute_local(records, name, args, relay, receipt)
            result = jsonable_encoder(output)
            if not schema_validator(function.contract.output_schema).is_valid(result):
                raise CapabilityError(
                    "Local result differs from the registered contract"
                )
        elif name in DATA_QUERIES and relay is not None:
            result = await relay.data_query(function, args, receipt["id"])
        else:
            raise CapabilityError(
                "Registered function has no available execution binding"
            )
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except WorkbenchError as exc:
        log.info("workbench_mcp_refused tool=%s code=%s", name, exc.code)
        # An exception alone cannot prove no nested service operation took place.
        return _tool_error(mid, exc.message), failure_status
    except Exception:
        log.exception("workbench_mcp_tool_failed tool=%s", name)
        return _tool_error(
            mid, "the service is temporarily unavailable"
        ), failure_status
    size = len(rendered.encode("utf-8"))
    if size > TOOL_RESULT_MAX_BYTES:
        if failure_status == "unknown":
            return _tool_error(
                mid, "Operation completed. Its output is too large to display."
            ), "succeeded"
        return _tool_error(
            mid,
            f"This result is too large to show in one go ({size} bytes); "
            "ask for a smaller part (another page, fewer items).",
        ), failure_status
    return _ok(
        mid,
        {
            "content": [{"type": "text", "text": rendered}],
            "structuredContent": result,
            "isError": False,
        },
    ), "succeeded"


router = APIRouter(
    prefix="/mcp/workbench",
    tags=["Workbench MCP"],
    dependencies=[Depends(require_workbench_enabled)],
)


def _unauthorized() -> Response:
    return JSONResponse(
        _err(None, -32001, "unauthorized"),
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("")
async def rpc(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    issuer: TokenIssuer | None = Depends(token_issuer),
    enabled: bool = Depends(records_enabled),
    per_minute: int = Depends(records_rate),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    if not enabled:
        return Response(status_code=404)
    scheme, _, token = (authorization or "").partition(" ")
    if issuer is None or scheme.lower() != "bearer" or not token or " " in token:
        return _unauthorized()
    identity = issuer.records_identity(token)
    if identity is None:
        return _unauthorized()
    owner, sandbox = identity
    try:
        body = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
    if body == []:
        return JSONResponse(_err(None, -32600, "invalid request"), status_code=400)
    records = ProjectRecords(workbench_store(session), owner_actor_id=owner)
    relay = ScopedDataFunctions(records, knowbase_v2(), issuer, sandbox=sandbox)
    guard = MethodToolGuard(
        records.repo,
        owner=owner,
        sandbox=sandbox,
        resource_delegations=get_settings().workbench_resource_delegations,
    )
    messages = body if isinstance(body, list) else [body]
    replies = []
    for message in messages:
        if not isinstance(message, dict):
            replies.append(_err(None, -32600, "invalid request"))
            continue
        reply = await handle(
            records,
            message,
            owner=owner,
            per_minute=per_minute,
            relay=relay,
            guard=guard,
        )
        if reply is not None:
            replies.append(reply)
    if not replies:
        return Response(status_code=202)
    payload: Any = replies if isinstance(body, list) else replies[0]
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("")
async def no_stream() -> Response:
    # 不提供服务端推送流；客户端应只用 POST
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("")
async def end_session() -> Response:
    return Response(status_code=204)
