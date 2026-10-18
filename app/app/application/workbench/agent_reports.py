"""本机权限决定的审计接收；回执不授予权限，权限只能由本机当面确认。"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.application.ports.workbench import WorkbenchStore

UUID_TEXT = r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$"
DIGEST_TEXT = r"^[0-9a-f]{64}$"


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"]
    network: bool


class PermissionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Annotated[str, Field(pattern=UUID_TEXT, min_length=36, max_length=36)]
    conn: Annotated[str, Field(pattern=r"^[0-9a-f]{8}$", min_length=8, max_length=8)]
    threadId: Annotated[str, Field(pattern=UUID_TEXT, min_length=36, max_length=36)]
    requestDigest: Annotated[
        str, Field(pattern=DIGEST_TEXT, min_length=64, max_length=64)
    ]
    permissionDigest: Annotated[
        str, Field(pattern=DIGEST_TEXT, min_length=64, max_length=64)
    ]
    decision: Literal["approved", "denied"]
    scope: Scope
    expiresAt: int

    @model_validator(mode="after")
    def mandatory_inner_sandbox(self):
        if self.scope.sandbox == "danger-full-access" and self.decision != "denied":
            raise ValueError("unsandboxed Windows permission cannot be approved")
        return self


class ReportEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt: Annotated[
        str, Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    ]
    report: PermissionReport


async def record_agent_reports(
    repo: WorkbenchStore, *, owner: str, environment_id: str, reports: Any
) -> list[dict[str, str]]:
    """调用方在事务内执行，提交后才把回执发回会合点；未知字段不入事件。"""
    if not isinstance(reports, list) or len(reports) > 32:
        return []
    receipts = []
    for raw in reports:
        try:
            envelope = ReportEnvelope.model_validate(raw)
        except ValidationError:
            # Never log the validation input: it may contain a credential.
            continue
        report = envelope.report
        status = "rejected"
        now = time.time()
        if now < report.expiresAt <= now + 7200:
            session = await repo.get_session_by_thread(report.threadId)
            if session is not None and str(session["owner_actor_id"]) == owner:
                # Serialise same-ID replay checks with concurrent sync workers.
                session = await repo.get_session(
                    str(session["id"]), owner_actor_id=owner, for_update=True
                )
            if (
                session is not None
                and str(session["owner_actor_id"]) == owner
                and str(session["environment_id"]) == environment_id
                and session["thread_id"] == report.threadId
            ):
                payload = report.model_dump()
                # Same report ID must not silently acquire a different scope.
                previous = await repo.list_record_events(
                    session_id=str(session["id"]), types=("agent/localPermission",)
                )
                matches = [e for e in previous if e["payload"].get("id") == report.id]
                if not matches or all(e["payload"] == payload for e in matches):
                    await repo.append_event_once(
                        session_id=str(session["id"]),
                        kind="record",
                        event_type="agent/localPermission",
                        payload=payload,
                        same={"id": report.id},
                    )
                    status = "recorded"
        receipts.append({"receipt": envelope.receipt, "status": status})
    return receipts
