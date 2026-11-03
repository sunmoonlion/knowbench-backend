"""工作台的工具服务：专家读本项目里别的对话与底稿（PRD/apps/investment.md 7.4），
专家读用户的数据（knowbase 数据 MCP 中继，0048）、计算（SDD 0030 §3.2）。

Streamable HTTP，只用 JSON 响应，不开流。和知识服务的工具并列，由沙箱里的 Codex 调用。
- 鉴权：`Authorization: Bearer <令牌>`，令牌由工作台签发、工作台自己验，绑定用户；
- 项目、计算的工具都要带 `task`（委托编号，专家每一步的说明里给）。能读哪个项目、
  现在能不能读，每次调用按这个委托重新核对；对话里读数据带对话编号（这一轮的说明里给）；
- 只读：没有改用户东西的工具。`compute` 只把算过的记进委托的计算记录（数值验收按它回查）。
- 读数据：knowbase 数据 MCP 的读工具照它的定义挂在这里（工具层中继，
  `data_relay.py`），带 `task` 或 `conversation`；第 0 步与运行中用 `request_data` 提出要
  什么（decisions ⑭），列进用户的确认清单，任务在步骤边界停下等确认；`find_subject`
  按名字查证券代码。
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench.data_relay import RELAYED, DataRelay
from app.application.workbench.expert_data import find_subject, request_data
from app.application.workbench.find_expert import find_expert
from app.application.workbench.knowbot_compute import KnowbotCompute
from app.application.workbench.method_tools import MethodToolGuard
from app.application.workbench.records import ProjectRecords
from app.application.workbench.run_reader import RunReader
from app.application.workbench.tokens import TokenIssuer
from app.bootstrap.workbench import (
    knowbase_v2,
    knowfeed_supply,
    knowmart_catalog,
    workbench_store,
)
from app.domain.workbench.compute import NAME as COMPUTE_NAME
from app.domain.workbench.data_request import DataRequest
from app.domain.workbench.errors import ComputeRefused, WorkbenchError
from app.domain.workbench.projects import TOOL_RESULT_MAX_BYTES
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.workbench_routes import (
    require_workbench_enabled,
    token_issuer,
)
from core.config import get_settings

log = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_KNOWFEED = {"name": "sunmoon-workbench", "version": "0.1.0"}

_TASK = {
    "type": "string",
    "description": "The task id given in the step instructions.",
}
_COMPUTE_FIELDS = {
    "name": {"type": "string", "maxLength": 200},
    "expression": {"type": "string", "maxLength": 500},
    "inputs": {
        "type": "object",
        "minProperties": 1,
        "maxProperties": 50,
        "propertyNames": {"pattern": COMPUTE_NAME.pattern},
        "additionalProperties": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
    },
    "round": {"type": "integer", "minimum": 0, "maximum": 10},
}

# Reuse the locked data-request/v1 contract instead of hiding its vocabulary and
# period fields behind unconstrained strings/objects in the model-facing tool.
_DATA_REQUEST_SCHEMA = DataRequest.model_json_schema()
_DATA_FIELDS = _DATA_REQUEST_SCHEMA["properties"]


TOOLS: dict[str, dict[str, Any]] = {
    "list_project_conversations": {
        "description": (
            "List the conversations of the project this task belongs to: id, kind "
            "(chat or work), title, when, number of turns and the first message. "
            "The current conversation is marked; it is already in your context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task": _TASK},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "read_project_conversation": {
        "description": (
            "Read one conversation of the project: who said what and what was done. "
            "Commands and file changes are summarised. Paged; ask for the next page "
            "when `pages` is greater than `page`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "conversation": {
                    "type": "string",
                    "description": "Id from list_project_conversations.",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["task", "conversation"],
            "additionalProperties": False,
        },
    },
    "discover_source_tools": {
        "description": "Discover registered data suppliers available to this task owner. Omit service to list domains, then pass one service to read its current tool input schemas. This does not fetch financial data. Use the exact route and arguments in request_data.source_query; user confirmation is still required. Supplier responses may contain annual report excerpts or links and are not necessarily complete PDFs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "service": {"type": "string", "maxLength": 64},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "find_subject": {
        "description": (
            "Look up the code of a listed company by its name, short name, pinyin "
            "initials or code (A shares on the Shanghai, Shenzhen and Beijing "
            "exchanges). Returns [{subject, name}], best match first. Use the "
            "subject as the code in request_data; never write a code from memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "name": {"type": "string", "minLength": 1, "maxLength": 40},
            },
            "required": ["task", "name"],
            "additionalProperties": False,
        },
    },
    "request_data": {
        "description": (
            "Propose the data this task needs (in step 0), or ask for data that is "
            "not pinned to it yet (for example one more year's annual report). It "
            "is listed for the user to confirm, change or drop; you cannot read it "
            "before that and the task pauses after this step. Each item: subject "
            "(a code from find_subject; leave it out for data about no particular "
            "company), doc_type (a type or class from describe_items, e.g. "
            "annual_report, semiannual_report, q1_report, q3_report; "
            "interim_announcement is NOT a periodic report). For explicit dates "
            "use period {kind: fixed, start, end}, including periodic reports; "
            "preserve the user's requested dates instead of replacing them with "
            "latest. Use {kind: latest, count} only for the latest periodic reports, "
            "or {kind: recent, months} for recent non-periodic material. "
            "reading (original, text, pages or table), optional "
            "topics and edition, essential (true if the steps cannot be done "
            "without it) and reason (why it is needed). At most 20 items. "
            "If a value is rejected, consult describe_items and correct that value; "
            "do not drop required data or substitute a different document type "
            "just to pass validation. State any additional baseline period separately. "
            "For a discovered supplier tool, set source_query={channel,service,tool,arguments}, "
            "doc_type=other_text, period={kind:none}, reading=text. Dates and exact data fields "
            "belong in tool arguments. This asks for original supplier response evidence, not "
            "an issuer PDF. Request report originals separately when page checks need them."
        ),
        "inputSchema": {
            "type": "object",
            "$defs": copy.deepcopy(_DATA_REQUEST_SCHEMA["$defs"]),
            "properties": {
                "task": _TASK,
                "needs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string", "maxLength": 64},
                            "doc_type": copy.deepcopy(_DATA_FIELDS["doc_type"]),
                            "source_query": copy.deepcopy(_DATA_FIELDS["source_query"]),
                            "period": copy.deepcopy(_DATA_FIELDS["period"]),
                            "reading": {"enum": ["original", "text", "pages", "table"]},
                            "topics": copy.deepcopy(_DATA_FIELDS["topics"]),
                            "edition": {"enum": ["full", "summary", "english"]},
                            "max_count": copy.deepcopy(_DATA_FIELDS["max_count"]),
                            "essential": {"type": "boolean"},
                            "reason": {"type": "string", "maxLength": 500},
                        },
                        "required": ["doc_type", "reason"],
                    },
                },
            },
            "required": ["task", "needs"],
            "additionalProperties": False,
        },
    },
    "find_expert": {
        # Business read only: the immutable directory receipt is audit evidence,
        # not an expert selection, delegation, purchase, or permission change.
        "annotations": {"readOnlyHint": True},
        "description": (
            "List the experts this user can ask on knowmart: knowbot_id, name, "
            "tagline, version, scope {does, does_not, elsewhere} exactly as each "
            "expert wrote it, price, and free (true when asking costs the user "
            "nothing extra). Only public information; never ranked. Match only by "
            "the experts' own scope against the user's question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task": _TASK},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "compute": {
        "description": (
            "Compute exact decimal results and record them. Use either the single "
            "name/expression/inputs form, or {task, batch: [{key, name, expression, "
            "inputs, round?}, ...]} for up to 50 related calculations and 500 total "
            "input references. Prefer a batch when facts are already available. "
            "Batch inputs may use batch:key to reference an earlier row; forward "
            "references are refused. Returns {results: [{key, id, value, formula, "
            "inputs}, ...]}; receipts are ordinary compute ids. The entire batch "
            "is refused if any row fails; split unrelated calculations if needed. "
            "Single-call `inputs` maps short "
            "names to fact refs (fact:… or raw:… from query_facts), to the id of an "
            "earlier computation of this task (e.g. c17), or to a list of them. "
            "Input names are case-sensitive ASCII identifiers, 1–32 characters: "
            "start with a letter or _, then letters, digits or _. For example, "
            "inputs {A: fact:…, L: fact:…, E: fact:…} uses expression A - (L + E). "
            "`expression` uses only those names, "
            "numbers, + - * / ( ), ** (fractional exponents require a positive base; "
            "absolute exponent at most 100, e.g. (end / start) ** (1 / years) - 1), "
            "and sum avg median min max abs (these take lists). "
            "You cannot pass numbers as inputs. Returns {id, value, formula, inputs}: "
            "put the id as compute_id next to every computed number you hand back. "
            "A zero denominator is refused: report the figure as not applicable. "
            "Ids are not consecutive (c3, c17, …); that is normal, nothing is missing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                **_COMPUTE_FIELDS,
                "batch": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "pattern": COMPUTE_NAME.pattern},
                            **_COMPUTE_FIELDS,
                        },
                        "required": ["key", "name", "expression", "inputs"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["task"],
            "oneOf": [
                {
                    "required": ["name", "expression", "inputs"],
                    "not": {"required": ["batch"]},
                },
                {
                    "required": ["batch"],
                    "not": {"anyOf": [{"required": [key]} for key in _COMPUTE_FIELDS]},
                },
            ],
            "additionalProperties": False,
        },
    },
}


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


async def call(
    records: ProjectRecords,
    name: str,
    args: dict[str, Any],
    reader: RunReader | None = None,
) -> Any:
    if name == "request_data":
        working = await records._working(args.get("task"))
        return await request_data(
            records.repo, knowbase_v2(), working, args.get("needs") or []
        )
    if name == "discover_source_tools":
        working = await records._working(args.get("task"))
        subject = await records.repo.subject_of_actor(str(working["owner_actor_id"]))
        if subject is None:
            return {"sources": []}
        return await knowfeed_supply().source_tools(subject, args.get("service"))
    if name == "find_subject":
        await records._working(args.get("task"))
        return await find_subject(knowfeed_supply(), args.get("name"))
    if name == "find_expert":
        # 找专家（请专家方案设计第 4.9 节）：凭这件「找专家」委托，查这个用户的 knowmart 目录
        working = await records._working(args.get("task"))
        return await find_expert(records.repo, knowmart_catalog(), working)
    if name == "compute":
        if "batch" in args:
            if set(args) - {"task", "batch"}:
                raise ComputeRefused(
                    "use either a single calculation or a batch, not both"
                )
            return await KnowbotCompute(records, reader).compute_batch(
                args.get("task"), args["batch"]
            )
        return await KnowbotCompute(records, reader).compute(
            args.get("task"),
            args.get("name"),
            args.get("expression"),
            args.get("inputs"),
            args.get("round"),
        )
    if name == "list_project_conversations":
        return await records.list_conversations(args.get("task"))
    if name == "read_project_conversation":
        return await records.read_conversation(
            args.get("task"), args.get("conversation"), args.get("page", 1)
        )
    raise ValueError(f"unknown tool {name}")


async def handle(
    records: ProjectRecords,
    message: dict[str, Any],
    *,
    owner: str,
    per_minute: int,
    relay: DataRelay | None = None,
    guard: MethodToolGuard | None = None,
) -> dict[str, Any] | None:
    method = message.get("method")
    mid = message.get("id")
    params = message.get("params") or {}
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
                    "Read-only records of the user's project, the user's data pinned "
                    "to this task or readable in this conversation, and exact "
                    "computation on that data. Pass the task id from the step "
                    "instructions, or in a chat the conversation id."
                ),
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        try:
            data_tools = await relay.tools() if relay is not None else []
        except WorkbenchError as exc:
            return _err(mid, -32003, exc.message)
        return _ok(
            mid,
            {
                "tools": [{"name": name, **spec} for name, spec in TOOLS.items()]
                + data_tools
            },
        )
    if method != "tools/call":
        return None if mid is None else _err(mid, -32601, f"method not found: {method}")
    name = str(params.get("name") or "")
    args = params.get("arguments") or {}
    relayed = relay is not None and name in RELAYED
    if name not in TOOLS and not relayed:
        return _err(mid, -32602, f"unknown tool: {name}")
    if not isinstance(args, dict):
        return _err(mid, -32602, "arguments must be an object")
    if not limiter.allow(owner, per_minute):
        return _tool_error(mid, "rate limit exceeded; retry later")
    if guard is not None:
        try:
            await guard.check(name, args, params.get("_meta"))
        except WorkbenchError as exc:
            log.info("workbench_mcp_refused tool=%s code=%s", name, exc.code)
            return _tool_error(mid, exc.message)
    if relayed:
        assert relay is not None
        try:
            return await relay.call(mid, name, args)
        except WorkbenchError as exc:
            log.info("workbench_mcp_refused tool=%s code=%s", name, exc.code)
            return _tool_error(mid, exc.message)
        except Exception:  # 内部错误的细节不给模型
            log.exception("workbench_mcp_relay_failed tool=%s", name)
            return _tool_error(mid, "the data service is temporarily unavailable")
    try:
        result = jsonable_encoder(
            await call(records, name, args, relay.reader if relay else None)
        )
    except WorkbenchError as exc:
        # 给模型看的话：不带编号之外的细节
        log.info("workbench_mcp_refused tool=%s code=%s", name, exc.code)
        return _tool_error(mid, exc.message)
    except Exception:  # 内部错误的细节不给模型
        log.exception("workbench_mcp_tool_failed tool=%s", name)
        return _tool_error(mid, "the records are temporarily unavailable")
    text = json.dumps(result, ensure_ascii=False)
    size = len(text.encode("utf-8"))
    if size > TOOL_RESULT_MAX_BYTES:
        # 太大会被 Codex 从中间省掉、模型却不知道：明说，让它换小一点的读法（待办账 137）
        log.warning("workbench_mcp_result_too_large tool=%s bytes=%d", name, size)
        return _tool_error(
            mid,
            f"This result is too large to show in one go ({size} bytes); "
            "ask for a smaller part (another page, fewer items).",
        )
    return _ok(
        mid,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": result,
            "isError": False,
        },
    )


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
    records = ProjectRecords(workbench_store(session), owner_actor_id=owner)
    relay = DataRelay(records, knowbase_v2(), issuer, sandbox=sandbox)
    guard = MethodToolGuard(records.repo, owner=owner, sandbox=sandbox)
    messages = body if isinstance(body, list) else [body]
    replies = []
    for message in messages:
        if isinstance(message, dict):
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
