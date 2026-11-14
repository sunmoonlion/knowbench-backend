"""User-facing capability management and scoped model tool endpoint."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.workbench import capability_service, web_workbench_store
from app.domain.security import Principal
from app.domain.workbench.capabilities import (
    ActionDecision,
    AssetAction,
    ConnectionCreate,
    ConnectionRenew,
    SelectionPut,
    SkillCreate,
)
from app.domain.workbench.capability_contract import (
    Asset,
    Call,
    Calls,
    Catalog,
    Selection,
)
from app.domain.workbench.errors import Forbidden, WorkbenchError
from app.domain.workbench.skill_execution import public_result
from app.domain.workbench.skill_installation import (
    SkillInstall,
    SkillSourcePreview,
    SkillSourceRequest,
)
from app.domain.workbench.skill_package import SkillPackageCreate, SkillRead
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.workbench_routes import (
    _actor,
    _http,
    require_workbench_enabled,
)
from app.interfaces.http.middleware.auth import get_web_current_user

router = APIRouter(
    tags=["Capabilities"], dependencies=[Depends(require_workbench_enabled)]
)


def service(db: AsyncSession = Depends(get_db_session)):
    return capability_service(web_workbench_store(db))


@router.post(
    "/workbench/capabilities/{asset_id}/renew",
    response_model=Asset,
    response_model_exclude_unset=True,
)
async def renew_connection(
    asset_id: UUID,
    body: ConnectionRenew,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.renew(_actor(user), str(asset_id), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get(
    "/workbench/capabilities", response_model=Catalog, response_model_exclude_unset=True
)
async def list_capabilities(
    user: Principal = Depends(get_web_current_user), svc=Depends(service)
):
    return {
        "assets": await svc.assets(_actor(user)),
        "services": [
            {"url": url, "authentication": ["none", "bearer"]}
            for url in svc.remote.allowed_urls
        ],
        "skill_formats": ["instruction-markdown", "skill-package-zip"],
        "runtime_enabled": bool(svc.runtime_url),
        "script_execution_enabled": svc.script_executor is not None,
    }


@router.post(
    "/workbench/capabilities/connections",
    status_code=201,
    response_model=Asset,
    response_model_exclude_unset=True,
)
async def create_connection(
    body: ConnectionCreate,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.create_connection(_actor(user), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/capabilities/skill-sources/preview", response_model=SkillSourcePreview
)
async def preview_skill_source(
    body: SkillSourceRequest,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.preview_skill_source(body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/capabilities/skill-sources/install",
    status_code=201,
    response_model=Asset,
    response_model_exclude_unset=True,
)
async def install_skill(
    body: SkillInstall,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.install_skill(_actor(user), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/capabilities/skills",
    status_code=201,
    response_model=Asset,
    response_model_exclude_unset=True,
)
async def create_skill(
    body: SkillCreate,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.create_skill(_actor(user), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/capabilities/skill-packages",
    status_code=201,
    response_model=Asset,
    response_model_exclude_unset=True,
)
async def create_skill_package(
    request: Request,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 3 * 1024 * 1024:
            return Response(status_code=413)
    try:
        body = SkillPackageCreate.model_validate_json(raw)
        return await svc.create_skill(_actor(user), body, package=True)
    except ValidationError:
        return JSONResponse(
            {"detail": "Invalid Skill package request"}, status_code=422
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/capabilities/{asset_id}/actions",
    response_model=Asset,
    response_model_exclude_unset=True,
)
async def asset_action(
    asset_id: UUID,
    body: AssetAction,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.action(_actor(user), str(asset_id), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get(
    "/workbench/sessions/{session_id}/capabilities",
    response_model=Selection,
    response_model_exclude_unset=True,
)
async def get_selection(
    session_id: UUID,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.selection(_actor(user), str(session_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.put(
    "/workbench/sessions/{session_id}/capabilities",
    response_model=Selection,
    response_model_exclude_unset=True,
)
async def put_selection(
    session_id: UUID,
    body: SelectionPut,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.save_selection(_actor(user), str(session_id), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get(
    "/workbench/sessions/{session_id}/capability-calls",
    response_model=Calls,
    response_model_exclude_unset=True,
)
async def list_calls(
    session_id: UUID,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        calls = await svc.calls(_actor(user), str(session_id))
        return {"calls": calls}
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/capability-calls/{call_id}/decision",
    response_model=Call,
    response_model_exclude_unset=True,
)
async def decide_call(
    call_id: UUID,
    body: ActionDecision,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.decide(_actor(user), str(call_id), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get("/workbench/capability-calls/{call_id}/artifacts/{index}")
async def script_artifact(
    call_id: UUID,
    index: int,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        call = await svc.repo.capability_call(_actor(user), str(call_id))
        result = call["detail"].get("result") or {}
        artifacts = result.get("artifacts", [])
        if call["detail"].get(
            "execution_kind"
        ) != "skill_script" or not 0 <= index < len(artifacts):
            return Response(status_code=404)
        artifact = artifacts[index]
        return Response(
            base64.b64decode(artifact["data_base64"], validate=True),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(PurePosixPath(artifact["path"]).name, safe=""),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/capability-calls/{call_id}/cancel",
    response_model=Call,
    response_model_exclude_unset=True,
)
async def stop_script(
    call_id: UUID, user: Principal = Depends(get_web_current_user), svc=Depends(service)
):
    try:
        return await svc.cancel_script(_actor(user), str(call_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post("/mcp/capabilities/{session_id}")
async def capability_rpc(
    session_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
    svc=Depends(service),
):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return Response(status_code=401)
    try:
        selected = await svc.runtime(str(session_id), token)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 70000:
                return Response(status_code=413)
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            return Response(status_code=400)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            return Response(status_code=202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sunmoon-capabilities", "version": "1"},
            }
        elif method == "tools/list":
            tools = svc.runtime_tools(selected["runtime"])
            result = {
                "tools": [
                    {
                        "name": key,
                        "description": "Request user approval for "
                        + spec["tool"]["name"]
                        + ". "
                        + spec["tool"]["description"]
                        + (
                            " This call waits for user approval and returns the actual operation result. Never claim success before receiving it."
                            if selected["runtime"].get("method")
                            or spec.get("execution_kind") == "skill_script"
                            else " This stages the operation only; use action_result after approval. Never claim success while awaiting approval."
                        ),
                        "inputSchema": spec["tool"]["inputSchema"],
                        # This broker only stages a local approval record. It does not
                        # run the remote operation or modify external resources.
                        "annotations": {
                            "readOnlyHint": False,
                            "destructiveHint": bool(selected["runtime"].get("method")),
                            "openWorldHint": bool(selected["runtime"].get("method")),
                        },
                    }
                    for key, spec in tools.items()
                ]
            }
            result["tools"].append(
                {
                    "name": "action_result",
                    "description": "Read the outcome of a previously requested external action in this conversation.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"call_id": {"type": "string"}},
                        "required": ["call_id"],
                        "additionalProperties": False,
                    },
                    "annotations": {"readOnlyHint": True},
                }
            )
            if not tools:
                result["tools"] = []
            if selected["runtime"].get("packages"):
                result["tools"].extend(
                    [
                        {
                            "name": "skill_files",
                            "description": "List files in a selected Skill package. Returns 50 paths per page; offset is a file index.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "skill_id": {"type": "string"},
                                    "offset": {"type": "integer", "minimum": 0},
                                },
                                "required": ["skill_id"],
                                "additionalProperties": False,
                            },
                            "annotations": {
                                "readOnlyHint": True,
                                "destructiveHint": False,
                                "openWorldHint": False,
                            },
                        },
                        {
                            "name": "skill_read",
                            "description": "Read a selected Skill package resource, without executing it. Use paths from skill_files. To follow a relative link set relative_to to its source document path. Text pagination offsets count characters; PNG/JPEG resources return an image.",
                            "inputSchema": SkillRead.model_json_schema(),
                            "annotations": {
                                "readOnlyHint": True,
                                "destructiveHint": False,
                                "openWorldHint": False,
                            },
                        },
                    ]
                )
        elif method == "tools/call":
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                return Response(status_code=400)
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise Forbidden("Invalid tool arguments")
            if params.get("name") in ("skill_files", "skill_read", "action_result"):
                await svc.verify_invocation(
                    str(session_id), token, params["name"], args, params.get("_meta")
                )
            if params.get("name") in ("skill_files", "skill_read"):
                listing = params["name"] == "skill_files"
                if listing:
                    if set(args) - {"skill_id", "offset"}:
                        raise Forbidden("Invalid skill_files arguments")
                    args = {**args, "path": "SKILL.md"}
                value = await svc.skill_resource(
                    str(session_id), token, args, listing=listing
                )
            elif params.get("name") == "action_result":
                await svc.repo.recover_calls(
                    selected["owner_actor_id"], str(session_id)
                )
                call = await svc.repo.capability_call(
                    selected["owner_actor_id"], str(UUID(args["call_id"]))
                )
                if call["session_id"] != str(session_id):
                    raise Forbidden("Call belongs to another conversation")
                if (
                    selected["runtime"].get("method")
                    and call["detail"].get("method") != selected["runtime"]["method"]
                ):
                    raise Forbidden("Call belongs to another method execution")
                value = {
                    "call_id": call["id"],
                    "status": call["status"],
                    "result": public_result(call["detail"].get("result"), call["id"]),
                }
            else:
                value = await svc.request_call(
                    str(session_id),
                    token,
                    params.get("name"),
                    args,
                    meta=params.get("_meta"),
                )
                spec = selected["runtime"].get("tools", {}).get(params.get("name"), {})
                if (
                    selected["runtime"].get("method")
                    or spec.get("execution_kind") == "skill_script"
                ):
                    # Keep the standard tools/call open while the user approves.
                    # SSE comments keep intermediaries alive; the final JSON-RPC
                    # response is emitted only when an actual outcome is known.
                    return StreamingResponse(
                        method_call_stream(
                            svc, str(session_id), token, value["call_id"], mid
                        ),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-store",
                            "X-Accel-Buffering": "no",
                        },
                    )
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
                ],
                "isError": False,
            }
            if isinstance(value, dict) and "image_base64" in value:
                image_data = value.pop("image_base64")
                result["content"] = [
                    {"type": "text", "text": json.dumps(value)},
                    {
                        "type": "image",
                        "data": image_data,
                        "mimeType": value["media_type"],
                    },
                ]
        else:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
        return JSONResponse(
            jsonable_encoder({"jsonrpc": "2.0", "id": mid, "result": result}),
            headers={"Cache-Control": "no-store"},
        )
    except WorkbenchError as exc:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": locals().get("mid"),
                "error": {"code": -32001, "message": exc.message},
            },
            status_code=403 if isinstance(exc, Forbidden) else 200,
        )
    except (ValueError, KeyError, TypeError):
        return Response(status_code=400)


async def method_call_stream(svc, session_id, token, call_id, mid):
    deadline = asyncio.get_running_loop().time() + 950
    while True:
        try:
            value = await svc.method_call_result(session_id, token, call_id)
        except WorkbenchError as exc:
            value = {
                "call_id": call_id,
                "status": "authorization_changed",
                "message": exc.message,
            }
        if value["status"] not in ("awaiting_approval", "dispatching"):
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
                ],
                "isError": value["status"] != "succeeded",
            }
            yield (
                "event: message\ndata: "
                + json.dumps({"jsonrpc": "2.0", "id": mid, "result": result})
                + "\n\n"
            )
            return
        if asyncio.get_running_loop().time() >= deadline:
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "Operation outcome is unknown; inspect its receipt before retrying.",
                    }
                ],
                "isError": True,
            }
            yield (
                "event: message\ndata: "
                + json.dumps({"jsonrpc": "2.0", "id": mid, "result": result})
                + "\n\n"
            )
            return
        yield ": waiting for operation decision/result\n\n"
        await asyncio.sleep(1)


@router.get("/mcp/capabilities/{session_id}")
async def no_stream(session_id: UUID):
    return Response(status_code=405)


@router.delete("/mcp/capabilities/{session_id}")
async def disconnect_mcp(session_id: UUID):
    # MCP transport close does not revoke the user's connection.
    return Response(status_code=204)
