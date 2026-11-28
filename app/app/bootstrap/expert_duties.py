"""Poll the persisted due periods. Locks and receipts are shared across runners."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.application.workbench.expert_duties import wake
from app.bootstrap.workbench import find_expert_service, knowbase_v2, workbench_store

log = logging.getLogger(__name__)


async def poll(sessions, stopping):
    while not stopping.is_set():
        try:
            async with sessions() as session:
                due = await workbench_store(session).due_duties(datetime.now(UTC))
            for row in due:
                if stopping.is_set():
                    break
                async with sessions() as session:
                    service = find_expert_service(session)
                    try:
                        await wake(
                            service,
                            str(row["owner_actor_id"]),
                            str(row["origin"]),
                            row["next_at"],
                            knowbase_v2(),
                        )
                    except Exception as exc:
                        reason = str(getattr(exc, "code", type(exc).__name__))[:100]
                        log.warning("expert duty %s blocked: %s", row["origin"], reason)
                        async with service.repo.transaction():
                            await service.repo.delay_duty(
                                str(row["origin"]),
                                row["next_at"],
                                datetime.now(UTC) + timedelta(minutes=5),
                                reason,
                                row["state_revision"],
                            )
        except Exception:
            log.exception("Could not poll expert duties")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=30)
        except TimeoutError:
            pass
