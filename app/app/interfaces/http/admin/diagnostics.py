from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workbench import cleanup_observation
from app.infrastructure.messaging.celery_producer import (
    CeleryNotConfiguredError,
    get_celery_producer,
)
from app.infrastructure.storage.postgres import get_db_session
from app.infrastructure.workbench.repository import WorkbenchRepository
from app.interfaces.http.middleware.auth import require_knowbench_admin
from core.config import get_settings

router = APIRouter(
    prefix="/admin/v1/diagnostics/tasks",
    tags=["Admin diagnostics"],
    dependencies=[Depends(require_knowbench_admin)],
)


@router.post("/ping", summary="Enqueue a diagnostic Celery ping")
async def enqueue_ping() -> dict[str, str]:
    producer = get_celery_producer()
    if not producer.enabled:
        raise HTTPException(status_code=503, detail="Celery producer not configured")
    try:
        task_id = producer.dispatch_ping()
    except CeleryNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"task_id": task_id, "queue": get_settings().celery_queue}


@router.get("/cleanup", summary="Pending metering and cleanup queues")
async def cleanup(response: Response, session: AsyncSession = Depends(get_db_session)):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        queues = await cleanup_observation.snapshot(WorkbenchRepository(session))
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Cleanup observation unavailable",
            headers={"Cache-Control": "private, no-store"},
        ) from None
    return {"alert_after_seconds": cleanup_observation.ALERT_SECONDS, "queues": queues}
