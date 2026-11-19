"""User-facing capability management and scoped model tool endpoint."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.workbench import capability_service, knowbase_v2, web_workbench_store
from app.domain.security import Principal
from app.domain.workbench.capabilities import (
    ActionDecision,
    AssetAction,
    ConnectionCreate,
    ConnectionRenew,
    MethodPermissionRevoke,
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
from app.domain.workbench.formal_method_calls import (
    FormalMethodCallRequest,
    FormalMethodCatalogResponse,
    FormalMethodInvocation,
    FormalMethodInvocations,
)
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
from app.interfaces.http.middleware.auth import (
    get_web_current_user,
    get_web_knowbase_token_provider,
)

router = APIRouter(
    tags=["Capabilities"], dependencies=[Depends(require_workbench_enabled)]
)


def service(db: AsyncSession = Depends(get_db_session)):
    return capability_service(web_workbench_store(db))


def formal_service(svc=Depends(service)):
    from app.application.workbench.formal_method_calls import FormalMethodCalls

    return FormalMethodCalls(svc)


@router.get(
    "/workbench/sessions/{session_id}/formal-methods",
    response_model=FormalMethodCatalogResponse,
)
async def available_formal_methods(
    session_id: UUID,
    response: Response,
    caller: Literal["host", "model"] = "host",
    user: Principal = Depends(get_web_current_user),
    svc=Depends(formal_service),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await svc.catalog(_actor(user), str(session_id), caller=caller)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post(
    "/workbench/sessions/{session_id}/formal-method-calls",
    response_model=FormalMethodInvocation,
)
async def request_formal_method(
    session_id: UUID,
    body: FormalMethodCallRequest,
    response: Response,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(formal_service),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await svc.request(_actor(user), str(session_id), body)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get(
    "/workbench/sessions/{session_id}/formal-method-calls",
    response_model=FormalMethodInvocations,
)
async def formal_method_call_history(
    session_id: UUID,
    response: Response,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(formal_service),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return {"calls": await svc.list_calls(_actor(user), str(session_id))}
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.get(
    "/workbench/sessions/{session_id}/formal-method-calls/{request_id}",
    response_model=FormalMethodInvocation,
)
async def formal_method_call_status(
    session_id: UUID,
    request_id: UUID,
    response: Response,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(formal_service),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await svc.status(_actor(user), str(session_id), str(request_id))
    except WorkbenchError as exc:
        raise _http(exc) from exc


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
            if not svc.knowbase_reader or url != svc.knowbase_reader.url
        ]
        + (
            [{"url": svc.knowbase_reader.url, "authentication": ["none"]}]
            if svc.knowbase_reader
            else []
        ),
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


class KnowbaseReadApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime


class ExistingParentApproval(KnowbaseReadApproval):
    grant_id: UUID


class PinnedParentApproval(KnowbaseReadApproval):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get("/workbench/capability-calls/{call_id}/knowbase-read")
async def preview_knowbase_read(
    call_id: UUID, user: Principal = Depends(get_web_current_user), svc=Depends(service)
):
    from app.application.workbench.knowbase_method_read import pending_read

    try:
        return await pending_read(svc, _actor(user), call_id)
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.post("/workbench/capability-calls/{call_id}/knowbase-read")
async def approve_knowbase_read(
    call_id: UUID,
    body: ExistingParentApproval | PinnedParentApproval,
    user: Principal = Depends(get_web_current_user),
    user_token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
    svc=Depends(service),
):
    from app.application.workbench.knowbase_method_read import pending_read
    from app.infrastructure.external.knowbase_client import KnowbaseRefused

    try:
        request = await pending_read(svc, _actor(user), call_id)
        if request["subject_digest"] != body.subject_digest:
            raise Forbidden("Consent does not match the displayed method read")
        token = await user_token()
        client = svc.knowbase_reader.client
        grant_id = (
            str(body.grant_id)
            if isinstance(body, ExistingParentApproval)
            else await client.method_parent_grant(
                token,
                request["read"],
                body.sha256,
                body.expires_at,
            )
        )
        await client.consent_method_read(
            token, grant_id, request["read"], body.expires_at
        )
        return {
            "grant_id": grant_id,
            "method_call_id": request["read"]["path"][-1]["call_id"],
            "expires_at": body.expires_at,
        }
    except KnowbaseRefused as exc:
        from app.application.errors.exceptions import AppException

        raise AppException(
            status_code=exc.status,
            code=exc.code,
            msg="KnowBase refused the method read consent",
        ) from exc
    except WorkbenchError as exc:
        raise _http(exc) from exc


@router.delete(
    "/workbench/knowbase-method-reads/{grant_id}/{method_call_id}", status_code=204
)
async def revoke_knowbase_read(
    grant_id: UUID,
    method_call_id: UUID,
    user_token: Callable[[], Awaitable[str]] = Depends(get_web_knowbase_token_provider),
    client=Depends(knowbase_v2),
):
    from app.application.errors.exceptions import AppException
    from app.infrastructure.external.knowbase_client import KnowbaseRefused

    try:
        await client.revoke_method_read(
            await user_token(), str(grant_id), str(method_call_id)
        )
    except KnowbaseRefused as exc:
        raise AppException(
            status_code=exc.status,
            code=exc.code,
            msg="KnowBase refused to revoke the method consent",
        ) from exc
    return Response(status_code=204)


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


@router.post(
    "/workbench/sessions/{session_id}/capabilities/revoke-method",
    response_model=Selection,
    response_model_exclude_unset=True,
)
async def revoke_method_permissions(
    session_id: UUID,
    body: MethodPermissionRevoke,
    user: Principal = Depends(get_web_current_user),
    svc=Depends(service),
):
    try:
        return await svc.revoke_method_permissions(_actor(user), str(session_id), body)
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
            from app.application.workbench.formal_session_tools import (
                FormalSessionTools,
            )

            tools = await FormalSessionTools(svc).available(
                selected["owner_actor_id"], selected["runtime"]
            )
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
                            or spec.get("execution_kind")
                            in {"skill_script", "formal_probe", "formal_method_model"}
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
            if selected["runtime"].get("formal_probe"):
                result["tools"] = [
                    t for t in result["tools"] if t["name"] != "action_result"
                ]
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
                if call["detail"].get("execution_kind") == "formal_method_model":
                    value = await svc.method_call_result(
                        str(session_id), token, call["id"]
                    )
                else:
                    value = {
                        "call_id": call["id"],
                        "status": call["status"],
                        "result": public_result(
                            call["detail"].get("result"), call["id"]
                        ),
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
                if selected["runtime"].get("method") or spec.get("execution_kind") in {
                    "skill_script",
                    "formal_probe",
                    "formal_method_model",
                }:
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
