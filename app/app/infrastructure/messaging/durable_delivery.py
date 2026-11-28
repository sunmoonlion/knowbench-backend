"""Template delivery policy: transaction ownership stays with application services.

A broker publish is a hint. Registered consumers acknowledge in PostgreSQL;
unacknowledged messages are recovered, bounded and explicitly replayable.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from app.infrastructure.messaging.delivery_schedule import NOT_BEFORE_DUE_SQL


class DeliveryLeaseLost(RuntimeError):
    pass


class DurableDelivery:
    active_execution_sql = """SELECT 1 FROM outbox_execution l
        WHERE l.message_id=m.id AND l.expires_at>clock_timestamp()"""

    def __init__(
        self,
        sessions,
        *,
        topics: dict[str, str | None],
        lease_seconds: int = 60,
        max_attempts: int = 10,
    ):
        if not 1 <= lease_seconds <= 3600 or not 1 <= max_attempts <= 100:
            raise ValueError("invalid delivery policy")
        self.sessions = sessions
        self.topics = dict(topics)
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    async def claim_delivery(self) -> dict[str, Any] | None:
        if not self.topics:
            return None
        async with self.sessions() as s, s.begin():
            row = (
                (
                    await s.execute(
                        text(f"""
                WITH candidate AS (
                    SELECT m.id FROM outbox_message m
                    WHERE m.topic = ANY(:topics)
                      AND m.available_at <= clock_timestamp()
                      AND {NOT_BEFORE_DUE_SQL}
                      AND (m.status='pending' OR (m.status='delivering'
                           AND m.lease_expires_at<=clock_timestamp()))
                      AND NOT EXISTS(SELECT 1 FROM outbox_execution l
                          WHERE l.resource_key=m.topic || ':' || m.aggregate_key
                            AND l.message_id<>m.id AND l.expires_at>clock_timestamp())
                      AND NOT EXISTS(SELECT 1 FROM outbox_dead_letter d
                          WHERE d.message_id=m.id AND d.replayed_at IS NULL)
                    ORDER BY m.created_at,m.id FOR UPDATE OF m SKIP LOCKED LIMIT 1
                )
                UPDATE outbox_message m SET status='delivering',lease_owner=:owner,
                    lease_expires_at=clock_timestamp()+(:ttl * interval '1 second'),
                    attempt_count=attempt_count+1,updated_at=clock_timestamp()
                FROM candidate c WHERE m.id=c.id RETURNING m.*
            """),
                        {
                            "topics": list(self.topics),
                            "owner": str(uuid.uuid4()),
                            "ttl": self.lease_seconds,
                        },
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def finish_delivery(self, message, *, error: str | None = None) -> None:
        async with self.sessions() as s, s.begin():
            row = await s.execute(
                text("""
                UPDATE outbox_message SET status=:status,
                    published_at=CASE WHEN :ok THEN clock_timestamp()
                        ELSE published_at END,
                    lease_owner=NULL,lease_expires_at=NULL,last_error=:error,
                    available_at=clock_timestamp()+(:delay * interval '1 second'),
                    updated_at=clock_timestamp()
                WHERE id=:id AND lease_owner=:owner AND status='delivering'
                    AND lease_expires_at>clock_timestamp() RETURNING id
            """),
                {
                    "id": message["id"],
                    "owner": message["lease_owner"],
                    "status": "pending" if error else "published",
                    "ok": error is None,
                    "error": error,
                    "delay": min(300, 2 ** min(message["attempt_count"], 8)),
                },
            )
            if row.scalar_one_or_none() is None:
                raise DeliveryLeaseLost("delivery lease lost")
            if error and message["attempt_count"] >= self.max_attempts:
                await self._dead_letter(s, message["id"], error)

    async def _dead_letter(self, s, message_id, error):
        # The caller holds the message lock. A matching business receipt also
        # counts if the publisher crashed before it recorded broker success.
        topic = await s.scalar(
            text("SELECT topic FROM outbox_message WHERE id=:id"), {"id": message_id}
        )
        consumer = self.topics.get(topic)
        if consumer and await s.scalar(
            text(
                "SELECT 1 FROM inbox_message "
                "WHERE message_id=:id AND consumer=:consumer"
            ),
            {"id": message_id, "consumer": consumer},
        ):
            await s.execute(
                text("""
                UPDATE outbox_message SET status='published',
                    published_at=COALESCE(published_at, clock_timestamp()),
                    lease_owner=NULL, lease_expires_at=NULL, last_error=NULL,
                    updated_at=clock_timestamp() WHERE id=:id
            """),
                {"id": message_id},
            )
            return
        await s.execute(
            text("""
            INSERT INTO outbox_dead_letter(message_id,error_code) VALUES (:id,:error)
            ON CONFLICT(message_id) DO UPDATE SET error_code=:error,
                failed_at=clock_timestamp(),replayed_at=NULL
        """),
            {"id": message_id, "error": error[:256]},
        )

    async def reconcile(self) -> int:
        count = 0
        async with self.sessions() as s, s.begin():
            # Bound processes repeatedly dying after claim but before recording publish.
            exhausted = (
                await s.execute(
                    text("""
                SELECT m.id FROM outbox_message m
                WHERE m.topic=ANY(:topics) AND m.attempt_count>=:attempts
                    AND (m.status='pending' OR (m.status='delivering'
                         AND m.lease_expires_at<=clock_timestamp()))
                    AND NOT EXISTS(SELECT 1 FROM outbox_execution l
                        WHERE l.message_id=m.id AND l.expires_at>clock_timestamp())
                    AND NOT EXISTS(SELECT 1 FROM outbox_dead_letter d
                        WHERE d.message_id=m.id AND d.replayed_at IS NULL)
                FOR UPDATE OF m SKIP LOCKED
            """),
                    {"topics": list(self.topics), "attempts": self.max_attempts},
                )
            ).all()
            for row in exhausted:
                await self._dead_letter(s, row[0], "delivery_attempts_exhausted")
            for topic, consumer in self.topics.items():
                if consumer is None:
                    continue  # Rebuildable hints have no business acknowledgement.
                rows = (
                    (
                        await s.execute(
                            text(f"""
                    SELECT m.id,m.attempt_count FROM outbox_message m
                    WHERE m.topic=:topic AND m.status='published'
                        AND m.published_at<clock_timestamp()
                            -(:ttl * interval '1 second')
                        AND NOT EXISTS(SELECT 1 FROM inbox_message i
                            WHERE i.consumer=:consumer AND i.message_id=m.id)
                        AND NOT EXISTS({self.active_execution_sql})
                        AND NOT EXISTS(SELECT 1 FROM outbox_dead_letter d
                            WHERE d.message_id=m.id AND d.replayed_at IS NULL)
                    FOR UPDATE OF m SKIP LOCKED
                """),
                            {
                                "topic": topic,
                                "consumer": consumer,
                                "ttl": self.lease_seconds,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    if row["attempt_count"] >= self.max_attempts:
                        await self._dead_letter(s, row["id"], "consumer_unacknowledged")
                    else:
                        await s.execute(
                            text("""
                            UPDATE outbox_message SET status='pending',
                                available_at='-infinity'::timestamptz,updated_at=clock_timestamp()
                            WHERE id=:id
                        """),
                            {"id": row["id"]},
                        )
                        count += 1
        return count

    async def replay(self, message_id: uuid.UUID) -> None:
        async with self.sessions() as s, s.begin():
            # Lock in the same order as claim/reconcile; keep committed inbox markers.
            row = (
                await s.execute(
                    text("""
                SELECT id FROM outbox_message WHERE id=:id AND topic=ANY(:topics)
                FOR UPDATE
            """),
                    {"id": message_id, "topics": list(self.topics)},
                )
            ).first()
            if not row:
                raise ValueError("message is outside this delivery policy")
            found = await s.execute(
                text("""
                UPDATE outbox_dead_letter SET replayed_at=clock_timestamp()
                WHERE message_id=:id AND replayed_at IS NULL RETURNING message_id
            """),
                {"id": message_id},
            )
            if found.scalar_one_or_none() is None:
                raise ValueError("message is not in the dead-letter journal")
            await s.execute(
                text("""
                UPDATE outbox_message SET status='pending',attempt_count=0,
                    available_at='-infinity'::timestamptz,last_error=NULL,
                    lease_owner=NULL,lease_expires_at=NULL,updated_at=clock_timestamp()
                WHERE id=:id
            """),
                {"id": message_id},
            )
