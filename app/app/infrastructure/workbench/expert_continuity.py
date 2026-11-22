import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ExpertContinuityRepositoryMixin:
    session: AsyncSession

    async def duty(self, origin, *, lock=False):
        suffix = " FOR UPDATE" if lock else ""
        result = await self.session.execute(
            text("SELECT * FROM workbench_expert_duties WHERE origin=:origin" + suffix),
            {"origin": origin},
        )
        return result.mappings().first()

    async def create_duty(self, origin, config, next_at):
        await self.session.execute(
            text("""
            INSERT INTO workbench_expert_duties (origin,current_match,config,next_at)
            VALUES (:id,:id,CAST(:config AS jsonb),:due)
        """),
            {"id": origin, "config": json.dumps(config), "due": next_at},
        )

    async def approve_duty_output(self, origin, pin):
        await self.session.execute(
            text("""UPDATE workbench_expert_duties
            SET approved=CAST(:pin AS jsonb), retry_at=NULL, problem=NULL WHERE origin=:id"""),
            {"id": origin, "pin": json.dumps(pin) if pin is not None else None},
        )

    async def pause_duty(self, origin, paused):
        await self.session.execute(
            text("""UPDATE workbench_expert_duties
            SET paused=:paused, retry_at=NULL, problem=NULL WHERE origin=:id"""),
            {"id": origin, "paused": paused},
        )

    async def advance_duty(self, origin, current_match, next_at):
        await self.session.execute(
            text("""UPDATE workbench_expert_duties
            SET current_match=:current, next_at=:due, approved=NULL, retry_at=NULL,
            problem=NULL WHERE origin=:id"""),
            {"id": origin, "current": current_match, "due": next_at},
        )

    async def due_duties(self, now):
        rows = await self.session.execute(
            text("""SELECT d.origin,d.next_at,t.owner_actor_id
            FROM workbench_expert_duties d JOIN workbench_tasks t ON t.id=d.origin
            WHERE NOT d.paused AND d.approved IS NOT NULL AND d.next_at<=:now
            AND (d.retry_at IS NULL OR d.retry_at<=:now) ORDER BY d.next_at,d.origin LIMIT 32"""),
            {"now": now},
        )
        return rows.mappings().all()

    async def delay_duty(self, origin, period, retry_at, problem):
        await self.session.execute(
            text("""UPDATE workbench_expert_duties
            SET retry_at=:retry, problem=:problem WHERE origin=:id AND next_at=:due
            AND NOT paused AND approved IS NOT NULL"""),
            {"id": origin, "due": period, "retry": retry_at, "problem": problem},
        )

    async def handoff_versions(self, request, previous, after):
        rows = await self.session.execute(
            text("""SELECT method_id,version,digest
            FROM workbench_formal_methods WHERE caller='knowlab'
            AND document#>>'{draft,definition,procedure,kind}'='orchestration'
            AND ((document#>>'{validation,request_id}'=:request)
                OR (method_id=CAST(:prior AS uuid) AND version>:old))
            AND (method_id,version) > (CAST(:cursor AS uuid), :version)
            ORDER BY method_id,version LIMIT 100"""),
            {
                "request": request,
                "prior": previous["method_id"] if previous else None,
                "old": previous["version"] if previous else 0,
                "cursor": after[0],
                "version": after[1],
            },
        )
        return [dict(r) for r in rows.mappings()]
