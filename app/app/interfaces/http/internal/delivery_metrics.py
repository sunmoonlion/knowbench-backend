"""Protected, bounded pull observation; never initializes or owns the API pool."""

from threading import BoundedSemaphore

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.application.errors.exceptions import ServiceUnavailableError
from app.infrastructure.messaging.delivery_observation import (
    collect_delivery_snapshot,
    render_prometheus,
)
from app.infrastructure.messaging.delivery_observers import get_delivery_observers
from app.infrastructure.storage.postgres import get_postgres
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.http.middleware.auth import require_internal_scopes

router = APIRouter(dependencies=[Depends(internal_host_only)])
# Immediate admission, not a queue of DB scans. Per process, not a cluster lock.
_collection_slot = BoundedSemaphore(1)


@router.get(
    "/internal/v1/delivery/metrics",
    dependencies=[Depends(require_internal_scopes("delivery:observe"))],
    response_class=Response,
)
async def delivery_metrics() -> Response:
    if not _collection_slot.acquire(blocking=False):
        raise ServiceUnavailableError(code="delivery_observation_busy")
    try:
        sessions = get_postgres().session_factory
        snapshot = await collect_delivery_snapshot(
            sessions, get_delivery_observers(sessions)
        )
        # Render completely before responding: no partial families or stale fallback.
        output = render_prometheus(snapshot)
        return Response(
            content=output,
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        # No SQL, parameters, credentials or original driver error in HTTP/logs.
        # CancelledError is deliberately not caught; finally still frees admission.
        raise ServiceUnavailableError(code="delivery_observation_failed") from None
    finally:
        _collection_slot.release()
