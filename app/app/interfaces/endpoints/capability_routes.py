"""User-facing capability management and scoped model tool endpoint."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
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
        "skill_formats": ["instruction-markdown"],
        "runtime_enabled": bool(svc.runtime_url),
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
            result = {
                "tools": [
                    {
                        "name": key,
                        "description": "Request user approval for "
                        + spec["tool"]["name"]
                        + ". "
                        + spec["tool"]["description"]
                        + " This stages the operation only; use action_result after approval. Never claim success while awaiting approval.",
                        "inputSchema": spec["tool"]["inputSchema"],
                        # This broker only stages a local approval record. It does not
                        # run the remote operation or modify external resources.
                        "annotations": {
                            "readOnlyHint": False,
                            "destructiveHint": False,
                            "openWorldHint": False,
                        },
                    }
                    for key, spec in selected["runtime"].get("tools", {}).items()
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
        elif method == "tools/call":
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                return Response(status_code=400)
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise Forbidden("Invalid tool arguments")
            if params.get("name") == "action_result":
                await svc.repo.capability_recover_calls(
                    selected["owner_actor_id"], str(session_id)
                )
                call = await svc.repo.capability_call(
                    selected["owner_actor_id"], str(UUID(args["call_id"]))
                )
                if call["session_id"] != str(session_id):
                    raise Forbidden("Call belongs to another conversation")
                value = {
                    "call_id": call["id"],
                    "status": call["status"],
                    "result": call["detail"].get("result"),
                }
            else:
                value = await svc.request_call(
                    str(session_id), token, params.get("name"), args
                )
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
                ],
                "isError": False,
            }
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


@router.get("/mcp/capabilities/{session_id}")
async def no_stream(session_id: UUID):
    return Response(status_code=405)


@router.delete("/mcp/capabilities/{session_id}")
async def disconnect_mcp(session_id: UUID):
    # MCP transport close does not revoke the user's connection.
    return Response(status_code=204)
