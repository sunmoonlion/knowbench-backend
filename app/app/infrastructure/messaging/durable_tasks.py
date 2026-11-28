"""Transactional task requests and fenced consumers on the PostgreSQL outbox."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.outbox import OutboxEvent
from app.infrastructure.messaging.delivery_schedule import NOT_BEFORE_DUE_SQL
from app.infrastructure.messaging.durable_delivery import (
    DeliveryLeaseLost,
    DurableDelivery,
)
from app.infrastructure.repositories.outbox import SqlOutboxRepository

Handler = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]


async def enqueue_task(
    session: AsyncSession,
    *,
    topic: str,
    key: str,
    payload: dict[str, Any],
    deduplication_key: str,
    not_before: datetime | None = None,
) -> uuid.UUID:
    """Caller commits domain state and this intent together; never calls the broker."""
    return await SqlOutboxRepository().enqueue(
        session,
        OutboxEvent(
            topic=topic,
            aggregate_key=key,
            payload=payload,
            deduplication_key=deduplication_key,
            not_before=not_before,
        ),
    )


@dataclass(frozen=True)
class ConsumerLease:
    resource_key: str
    message_id: uuid.UUID
    owner: uuid.UUID
    epoch: int


CHECK_LEASE = text("""
    SELECT epoch FROM outbox_execution WHERE resource_key=:resource_key
        AND message_id=:message_id AND owner=:owner AND epoch=:epoch
        AND expires_at>clock_timestamp() FOR UPDATE
""")


async def assert_execution_current(session: AsyncSession) -> None:
    """Check before external effects; every commit also enforces the lease."""
    lease = session.info.get("delivery_lease")
    if (
        lease
        and (
            await session.execute(
                text(str(CHECK_LEASE).replace(" FOR UPDATE", "")), vars(lease)
            )
        ).first()
        is None
    ):
        raise DeliveryLeaseLost("consumer lease lost")


class DurableTasks(DurableDelivery):
    def __init__(self, sessions, *, handlers: dict[str, Handler], **kwargs):
        super().__init__(
            sessions, topics={topic: topic for topic in handlers}, **kwargs
        )
        self.handlers = dict(handlers)

    async def claim_execution(self, message_id: uuid.UUID):
        async with self.sessions() as s, s.begin():
            row = (
                (
                    await s.execute(
                        text(f"""
                SELECT m.* FROM outbox_message m WHERE m.id=:id AND m.topic=ANY(:topics)
                    AND {NOT_BEFORE_DUE_SQL}
                    AND NOT EXISTS(SELECT 1 FROM inbox_message i
                        WHERE i.consumer=m.topic AND i.message_id=m.id)
                    AND NOT EXISTS(SELECT 1 FROM outbox_dead_letter d
                        WHERE d.message_id=m.id AND d.replayed_at IS NULL)
                FOR UPDATE OF m
            """),
                        {"id": message_id, "topics": list(self.handlers)},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            resource = row["topic"] + ":" + row["aggregate_key"]
            owner = uuid.uuid4()
            epoch = (
                await s.execute(
                    text("""
                INSERT INTO outbox_execution
                    (resource_key,message_id,owner,epoch,expires_at)
                VALUES (:key,:id,:owner,1,
                    clock_timestamp()+(:ttl * interval '1 second'))
                ON CONFLICT(resource_key) DO UPDATE SET message_id=:id,owner=:owner,
                    epoch=outbox_execution.epoch+1,
                    expires_at=clock_timestamp()+(:ttl * interval '1 second')
                WHERE outbox_execution.expires_at<=clock_timestamp() RETURNING epoch
            """),
                    {
                        "key": resource,
                        "id": message_id,
                        "owner": owner,
                        "ttl": self.lease_seconds,
                    },
                )
            ).scalar_one_or_none()
            if epoch is None:
                # A different message owns the business resource. Refund this
                # delivery once; changing status also fences a late publisher.
                await s.execute(
                    text("""
                    UPDATE outbox_message m SET status='pending',
                        attempt_count=GREATEST(0,attempt_count-1),
                        lease_owner=NULL, lease_expires_at=NULL, last_error=NULL,
                        available_at=clock_timestamp(), updated_at=clock_timestamp()
                    WHERE m.id=:id AND m.status IN ('delivering','published')
                      AND EXISTS(SELECT 1 FROM outbox_execution l
                        WHERE l.resource_key=:key AND l.message_id<>m.id)
                """),
                    {"id": message_id, "key": resource},
                )
                return None
            # Previous execution could have committed while we waited on its row lock.
            done = (
                await s.execute(
                    text("""
                SELECT 1 FROM inbox_message WHERE consumer=:topic AND message_id=:id
            """),
                    {"topic": row["topic"], "id": message_id},
                )
            ).first()
            if done:
                await s.execute(
                    text("""
                    UPDATE outbox_execution SET expires_at='-infinity'::timestamptz
                    WHERE resource_key=:key
                """),
                    {"key": resource},
                )
                return None
            return ConsumerLease(resource, message_id, owner, epoch), dict(row)

    async def renew(self, lease: ConsumerLease) -> None:
        async with self.sessions() as s, s.begin():
            row = await s.execute(
                text("""
                UPDATE outbox_execution
                SET expires_at=clock_timestamp()+(:ttl * interval '1 second')
                WHERE resource_key=:resource_key AND message_id=:message_id
                    AND owner=:owner AND epoch=:epoch AND expires_at>clock_timestamp()
                RETURNING epoch
            """),
                {**vars(lease), "ttl": self.lease_seconds},
            )
            if row.first() is None:
                raise DeliveryLeaseLost("consumer lease lost")

    async def release(self, lease: ConsumerLease) -> None:
        # Released is a state, not a wall-clock deadline. Keep the epoch tombstone;
        # a clock correction must neither resurrect this owner nor delay a retry.
        async with self.sessions() as s, s.begin():
            await s.execute(
                text("""
                UPDATE outbox_execution SET expires_at='-infinity'::timestamptz
                WHERE resource_key=:resource_key AND message_id=:message_id
                    AND owner=:owner AND epoch=:epoch
            """),
                vars(lease),
            )

    async def consume(self, message_id: uuid.UUID) -> bool:
        claimed = await self.claim_execution(message_id)
        if claimed is None:
            return False
        lease, message = claimed
        async with self.sessions() as s:
            s.info["delivery_lease"] = lease

            def guard(sync_session):
                if sync_session.execute(CHECK_LEASE, vars(lease)).first() is None:
                    raise DeliveryLeaseLost("consumer lease lost")

            # Covers domain services that commit intermediate progress themselves.
            event.listen(s.sync_session, "before_commit", guard)

            async def execute():
                await assert_execution_current(s)
                await s.rollback()  # Do not hold the lease row across remote calls.
                await self.handlers[message["topic"]](s, message["payload"])
                await SqlOutboxRepository.claim_inbox_once(
                    s, consumer=message["topic"], message_id=message_id
                )
                await s.commit()

            async def heartbeat():
                while True:
                    await asyncio.sleep(self.lease_seconds / 3)
                    await self.renew(lease)

            work = asyncio.create_task(execute())
            renewal = asyncio.create_task(heartbeat())
            try:
                done, _ = await asyncio.wait(
                    {work, renewal}, return_when=asyncio.FIRST_COMPLETED
                )
                if renewal in done:
                    await renewal
                await work
                return True
            finally:
                for task in (work, renewal):
                    task.cancel()
                for task in (work, renewal):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                await s.rollback()
                event.remove(s.sync_session, "before_commit", guard)
                await self.release(lease)
