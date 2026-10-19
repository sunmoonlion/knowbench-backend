"""工作台的工具服务：专家读本项目里别的对话与底稿（PRD/apps/investment.md 7.4），
分身读用户自己绑定的数据（SDD 0027 §3.5）、按格子读与计算（SDD 0030 §3.1、§3.2）。

Streamable HTTP，只用 JSON 响应，不开流。和知识服务的工具并列，由沙箱里的 Codex 调用。
- 鉴权：`Authorization: Bearer <令牌>`，令牌由工作台签发、工作台自己验，绑定用户；
- 每个工具都要带 `task`（委托编号，专家每一步的说明里给）。能读哪个项目、现在能不能读，
  每次调用按这个委托重新核对；
- 只读：没有改用户东西的工具。`compute` 只把算过的记进委托的计算记录（数值验收按它回查）。
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

from app.application.workbench.knowbot_compute import KnowbotCompute
from app.application.workbench.knowbot_data import KnowbotData
from app.application.workbench.library import Library
from app.application.workbench.records import ProjectRecords
from app.application.workbench.tokens import TokenIssuer
from app.bootstrap.workbench import knowbot_data_source, workbench_store
from app.domain.workbench.errors import WorkbenchError
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
    # 知识库（SDD 0011）：用户在我们这里的资料，跨项目。不需要 task；给了就把这次读记在那个委托的账上
    "list_library": {
        "description": (
            "List the user's library: working papers the experts handed back and the "
            "deliverables of each step, across all projects. Each item has an id, kind "
            "(dossier or deliverable), title, project, number of versions and the "
            "question it came from. Optional filters: kind, q (title contains)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    **_TASK,
                    "description": "Optional. The task id if you are working on one.",
                },
                "kind": {"type": "string", "enum": ["dossier", "deliverable"]},
                "q": {"type": "string", "maxLength": 200},
            },
            "additionalProperties": False,
        },
    },
    "read_library_item": {
        "description": (
            "Read one library item as text (a dossier as Markdown; a deliverable as its "
            "content). Paged; ask for the next page when `pages` is greater than `page`. "
            "The reply carries a citation {library_item_id, version, sha256}: quote it "
            "when you use the content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    **_TASK,
                    "description": "Optional. The task id if you are working on one.",
                },
                "item": {"type": "string", "description": "Id from list_library."},
                "version": {"type": "integer", "minimum": 1},
                "page": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["item"],
            "additionalProperties": False,
        },
    },
    # 分身读用户自己的数据（0027 §3.5）：只有这一版清单绑定的那几份，经 knowlab 读出文字
    "list_my_data": {
        "description": (
            "List the user's own data bound to this expert: dataset_id, name, "
            "content_type, size_bytes, version, sha256. Only these can be read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"task": _TASK},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    "read_my_data": {
        "description": (
            "Read one of the user's datasets as text (CSV as is; Excel as CSV per "
            "sheet; PDF page by page). Paged; ask for the next page when `pages` is "
            "greater than `page`. Quote the citation {dataset_id, version, sha256, "
            "page} for every number you take from it. If `note` says the text was "
            "cut or has no readable text, say so instead of guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "dataset": {"type": "string", "description": "Id from list_my_data."},
                "page": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["task", "dataset"],
            "additionalProperties": False,
        },
    },
    # 按格子读与计算（0030）：每个算出来的数都由 compute 算，输入按格子地址取，模型不抄数
    "read_my_table": {
        "description": (
            "Read one sheet of a CSV or Excel dataset as labelled cells: `columns` are "
            "the header labels (dates as YYYY-MM-DD), each row has its `label` (first "
            "column; a repeated label gets #2, #3; so does a repeated column label) and "
            "its values. Use it to write cell addresses for compute: "
            "<dataset_id>:<sheet>!<row label>@<column label>, "
            "e.g. 3f…:利润表!营业收入@2024-06-30. Copy the labels exactly as returned "
            "here; do not reformat dates. Paged like read_my_data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "dataset": {"type": "string", "description": "Id from list_my_data."},
                "sheet": {
                    "type": "string",
                    "description": "Sheet name from `sheets`; the first sheet if omitted.",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["task", "dataset"],
            "additionalProperties": False,
        },
    },
    "compute": {
        "description": (
            "Compute one number exactly (decimal) and record it. `inputs` maps short "
            "names to cell addresses, to the id of an earlier computation of this task "
            "(e.g. c17), or to a list of them. `expression` uses only those names, "
            "numbers, + - * / ( ) and sum avg median min max abs (these take lists). "
            "You cannot pass numbers as inputs. Returns {id, value, formula, inputs}: "
            "put the id as compute_id next to every computed number you hand back. "
            "A zero denominator is refused: report the figure as not applicable. "
            "Ids are not consecutive (c3, c17, …); that is normal, nothing is missing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": _TASK,
                "name": {"type": "string", "maxLength": 200},
                "expression": {"type": "string", "maxLength": 500},
                "inputs": {
                    "type": "object",
                    "additionalProperties": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                },
                "round": {"type": "integer", "minimum": 0, "maximum": 10},
            },
            "required": ["task", "name", "expression", "inputs"],
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


async def _library_call(
    records: ProjectRecords, name: str, args: dict[str, Any]
) -> Any:
    library = Library(records.repo, owner_actor_id=records.owner)
    # 在委托里读的，记到那个委托的账上（F-LIB-06）；聊天里读的没有委托，不记
    working = await records._working(args["task"]) if args.get("task") else None
    if name == "list_library":
        result = await library.tool_list(kind=args.get("kind"), q=args.get("q"))
    else:
        result = await library.tool_read(
            args.get("item"), version=args.get("version"), page=args.get("page", 1)
        )
    if working is not None:
        await records._noted(working, name, item=args.get("item"))
    return result


async def call(records: ProjectRecords, name: str, args: dict[str, Any]) -> Any:
    if name in ("list_library", "read_library_item"):
        return await _library_call(records, name, args)
    if name == "list_my_data":
        return await KnowbotData(records, knowbot_data_source()).list(args.get("task"))
    if name == "read_my_data":
        return await KnowbotData(records, knowbot_data_source()).read(
            args.get("task"), args.get("dataset"), args.get("page", 1)
        )
    if name == "read_my_table":
        return await KnowbotData(records, knowbot_data_source()).table(
            args.get("task"),
            args.get("dataset"),
            args.get("sheet"),
            args.get("page", 1),
        )
    if name == "compute":
        return await KnowbotCompute(records, knowbot_data_source()).compute(
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
    records: ProjectRecords, message: dict[str, Any], *, owner: str, per_minute: int
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
                    "Read-only records of the user's project, the user's library and "
                    "the user's own data bound to this expert, and exact computation "
                    "on that data. Project, data and compute tools need the task id "
                    "from the step instructions; the library tools work without it."
                ),
            },
        )
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(
            mid, {"tools": [{"name": name, **spec} for name, spec in TOOLS.items()]}
        )
    if method != "tools/call":
        return None if mid is None else _err(mid, -32601, f"method not found: {method}")
    name = str(params.get("name") or "")
    args = params.get("arguments") or {}
    if name not in TOOLS:
        return _err(mid, -32602, f"unknown tool: {name}")
    if not isinstance(args, dict):
        return _err(mid, -32602, "arguments must be an object")
    if not limiter.allow(owner, per_minute):
        return _tool_error(mid, "rate limit exceeded; retry later")
    try:
        result = jsonable_encoder(await call(records, name, args))
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
            "ask for a smaller part (another page, one sheet, fewer cells).",
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
    owner = issuer.owner_of_records_token(token)
    if owner is None:
        return _unauthorized()
    try:
        body = json.loads(await request.body())
    except ValueError:
        return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
    records = ProjectRecords(workbench_store(session), owner_actor_id=owner)
    messages = body if isinstance(body, list) else [body]
    replies = []
    for message in messages:
        if isinstance(message, dict):
            reply = await handle(records, message, owner=owner, per_minute=per_minute)
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
