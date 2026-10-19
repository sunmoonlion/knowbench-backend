"""工作台仓储。与旧 AgentRepository 平行；不共用 agent_* 表。

约定：
- 每个公开方法要么在调用方的 transaction() 里跑，要么自己 @atomic；
- 事件游标按会话用咨询锁串行分配（与 session_events 同法）；
- 状态改动一律 compare-and-swap（where state_version = :expected），失败抛 StaleStateVersion；
- 不在这里判断业务合法性（转换表在 domain），这里只保证原子性。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import wraps
from typing import Any

from sqlalchemy import text
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.dto.outbox import OutboxEvent
from app.domain.workbench.errors import (
    KnowbotInvalid,
    NotFound,
    ProjectBusy,
    ProjectExists,
    RelayIdentityBusy,
    StaleStateVersion,
)
from app.domain.workbench.states import TURN_HOLD_SECONDS
from app.domain.workbench.tokens import token_hash
from app.infrastructure.repositories.outbox import SqlOutboxRepository


def atomic(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        async with self.transaction():
            return await method(self, *args, **kwargs)

    return wrapped


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _event(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k in ("id", "session_id", "task_id", "attempt_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


# 占着项目的委托（同一项目一次一位，0017）：没结束的（排在别人后面的不算），以及刚结束、
# Codex 里那一轮还没确认停下的（`turn_open`，最多 TURN_HOLD_SECONDS；SDD 0034 §三.1）。
# 提交、取消、放行、对账都按这一条判，别的地方不另写
_OCCUPYING = """(
    (a.state not in ('SUCCEEDED', 'REJECTED', 'FAILED', 'CANCELLED')
     and not (a.state = 'RECEIVED'
              and coalesce(a.original_input->'client_context'->>'queued_behind', '') <> ''))
    or (a.state in ('SUCCEEDED', 'REJECTED', 'FAILED', 'CANCELLED')
        and a.original_input->'client_context'->'turn_open' is not null
        and (a.original_input->'client_context'->'turn_open'->>'since')::timestamptz
            > now() - make_interval(secs => :hold)))"""


class WorkbenchRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
        self._depth = 0

    @asynccontextmanager
    async def transaction(self):
        outer = self._depth == 0
        self._depth += 1
        try:
            yield
            if outer:
                await self.session.commit()
        except BaseException:
            if outer:
                await self.session.rollback()
            raise
        finally:
            self._depth -= 1

    async def commit(self) -> None:
        await self.session.commit()

    async def flush(self) -> None:
        await self.session.flush()

    # ---------- environments / sandboxes ----------
    async def register_environment(
        self,
        *,
        owner_actor_id: str,
        name: str,
        agent_version: str | None,
        codex_version: str | None,
        roots: list[str],
        ceiling: dict,
    ) -> str:
        env_id = str(uuid.uuid4())
        await self.session.execute(
            text("""insert into workbench_environments (id, owner_actor_id, name, agent_version, codex_version, roots, ceiling, status, last_seen_at)
                    values (:id, :owner, :name, :av, :cv, cast(:roots as jsonb), cast(:ceiling as jsonb), 'online', now())"""),
            {
                "id": env_id,
                "owner": owner_actor_id,
                "name": name,
                "av": agent_version,
                "cv": codex_version,
                "roots": _j(roots),
                "ceiling": _j(ceiling),
            },
        )
        return env_id

    async def list_environments(self, *, owner_actor_id: str) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select * from workbench_environments where owner_actor_id = :o order by created_at"
            ),
            {"o": owner_actor_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def get_environment(
        self, environment_id: str, *, owner_actor_id: str | None = None
    ) -> dict[str, Any]:
        r = await self.session.execute(
            text("select * from workbench_environments where id = :id"),
            {"id": environment_id},
        )
        row = r.mappings().first()
        if row is None or (
            owner_actor_id is not None and str(row["owner_actor_id"]) != owner_actor_id
        ):
            raise NotFound("environment not found", environment_id=environment_id)
        return dict(row)

    async def set_environment_status(self, environment_id: str, status: str) -> None:
        await self.session.execute(
            text(
                "update workbench_environments set status = :s, last_seen_at = now(), updated_at = now() where id = :id"
            ),
            {"s": status, "id": environment_id},
        )

    async def report_environment(
        self,
        *,
        owner_actor_id: str,
        name: str,
        agent_version: str | None,
        codex_version: str | None,
        roots: list[str],
        ceiling: dict,
    ) -> str:
        r = await self.session.execute(
            text(
                """select id from workbench_environments where owner_actor_id = :o and name = :n
                   order by created_at limit 1 for update"""
            ),
            {"o": owner_actor_id, "n": name},
        )
        row = r.first()
        if row is None:
            return await self.register_environment(
                owner_actor_id=owner_actor_id,
                name=name,
                agent_version=agent_version,
                codex_version=codex_version,
                roots=roots,
                ceiling=ceiling,
            )
        await self.session.execute(
            text(
                """update workbench_environments set agent_version = :av, codex_version = :cv,
                     roots = cast(:roots as jsonb), ceiling = cast(:ceiling as jsonb),
                     status = 'online', last_seen_at = now(), updated_at = now() where id = :id"""
            ),
            {
                "id": row[0],
                "av": agent_version,
                "cv": codex_version,
                "roots": _j(roots),
                "ceiling": _j(ceiling),
            },
        )
        return str(row[0])

    async def set_environments_offline(
        self, owner_actor_id: str, *, except_id: str | None = None
    ) -> int:
        # last_seen_at 不动：它记的是最后一次在线的时刻
        r = await self.session.execute(
            text(
                """update workbench_environments set status = 'offline', updated_at = now()
                   where owner_actor_id = :o and status = 'online'
                     and (cast(:x as uuid) is null or id <> cast(:x as uuid)) returning id"""
            ),
            {"o": owner_actor_id, "x": except_id},
        )
        return len(r.all())

    async def list_relay_identities(self) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select owner_actor_id, relay_user from workbench_relay_identities order by created_at"
            )
        )
        return [dict(m) for m in r.mappings().all()]

    async def register_sandbox(
        self,
        *,
        owner_actor_id: str,
        app_server_url: str,
        token_ref: str,
        codex_version: str | None,
    ) -> str:
        sb_id = str(uuid.uuid4())
        await self.session.execute(
            text("""insert into workbench_sandboxes (id, owner_actor_id, app_server_url, token_ref, codex_version, status)
                    values (:id, :owner, :url, :ref, :cv, 'registered')"""),
            {
                "id": sb_id,
                "owner": owner_actor_id,
                "url": app_server_url,
                "ref": token_ref,
                "cv": codex_version,
            },
        )
        return sb_id

    async def get_sandbox(
        self, sandbox_id: str, *, owner_actor_id: str | None = None
    ) -> dict[str, Any]:
        r = await self.session.execute(
            text("select * from workbench_sandboxes where id = :id"), {"id": sandbox_id}
        )
        row = r.mappings().first()
        if row is None or (
            owner_actor_id is not None and str(row["owner_actor_id"]) != owner_actor_id
        ):
            raise NotFound("sandbox not found", sandbox_id=sandbox_id)
        return dict(row)

    async def list_sandboxes(self, *, owner_actor_id: str) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select * from workbench_sandboxes where owner_actor_id = :o order by created_at"
            ),
            {"o": owner_actor_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def current_sandbox(self, owner_actor_id: str) -> dict[str, Any] | None:
        """这个人现在用的沙箱：按需拉起的优先，其次是最近登记的；回收了的不算。"""
        r = await self.session.execute(
            text(
                """select * from workbench_sandboxes where owner_actor_id = :o and status <> 'deleted'
                   order by provisioned desc, updated_at desc, created_at desc limit 1"""
            ),
            {"o": owner_actor_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    # ---------- projects ----------
    async def create_project(
        self,
        *,
        owner_actor_id: str,
        environment_id: str,
        workspace_root: str,
        path: str,
        title: str,
    ) -> str:
        project_id = str(uuid.uuid4())
        try:
            # 保存点：撞上唯一索引时只回退这一句，调用方的事务还能接着用
            async with self.session.begin_nested():
                await self.session.execute(
                    text(
                        """insert into workbench_projects (id, owner_actor_id, environment_id, workspace_root, path, title)
                           values (:id, :o, :e, :r, :p, :t)"""
                    ),
                    {
                        "id": project_id,
                        "o": owner_actor_id,
                        "e": environment_id,
                        "r": workspace_root,
                        "p": path,
                        "t": title,
                    },
                )
        except IntegrityError as exc:
            if "uq_workbench_projects_live_dir" in str(exc.orig):
                raise ProjectExists(
                    "a project already exists for this directory"
                ) from None
            raise
        return project_id

    async def get_project(
        self,
        project_id: str,
        *,
        owner_actor_id: str | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]:
        try:
            key = str(uuid.UUID(str(project_id)))
        except ValueError:
            raise NotFound("project not found", project_id=project_id) from None
        sql = "select * from workbench_projects where id = :id" + (
            " for update" if for_update else ""
        )
        row = (await self.session.execute(text(sql), {"id": key})).mappings().first()
        if row is None or (
            owner_actor_id is not None and str(row["owner_actor_id"]) != owner_actor_id
        ):
            raise NotFound("project not found", project_id=project_id)
        return dict(row)

    async def find_project(
        self,
        *,
        owner_actor_id: str,
        environment_id: str,
        workspace_root: str,
        path: str,
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select * from workbench_projects where owner_actor_id = :o and environment_id = :e
                   and workspace_root = :r and path = :p and archived_at is null"""
            ),
            {"o": owner_actor_id, "e": environment_id, "r": workspace_root, "p": path},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def list_projects(
        self, *, owner_actor_id: str, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """select p.*,
                          (select count(*) from workbench_sessions s where s.project_id = p.id and s.archived_at is null) as conversations,
                          (select max(s.last_active_at) from workbench_sessions s where s.project_id = p.id and s.archived_at is null) as last_active_at
                   from workbench_projects p where p.owner_actor_id = :o"""
                + ("" if include_archived else " and p.archived_at is null")
                + " order by p.workspace_root, p.path"
            ),
            {"o": owner_actor_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def update_project(
        self,
        project_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> None:
        try:
            async with self.session.begin_nested():
                if title is not None:
                    await self.session.execute(
                        text(
                            "update workbench_projects set title = :t, updated_at = now() where id = :id"
                        ),
                        {"t": title, "id": project_id},
                    )
                if archived is not None:
                    await self.session.execute(
                        text(
                            "update workbench_projects set archived_at = "
                            + ("now()" if archived else "null")
                            + ", updated_at = now() where id = :id"
                        ),
                        {"id": project_id},
                    )
        except IntegrityError as exc:
            if "uq_workbench_projects_live_dir" in str(exc.orig):
                raise ProjectExists(
                    "another project already uses this directory"
                ) from None
            raise

    # ---------- sessions ----------
    async def create_session(
        self,
        *,
        owner_actor_id: str,
        environment_id: str | None,
        sandbox_id: str,
        project_root: str | None,
        thread_settings: dict,
        kind: str = "work",
        project_id: str | None = None,
        title: str | None = None,
    ) -> str:
        sid = str(uuid.uuid4())
        await self.session.execute(
            text("""insert into workbench_sessions
                        (id, owner_actor_id, environment_id, sandbox_id, project_root, thread_settings,
                         kind, project_id, title)
                    values (:id, :owner, :env, :sb, :root, cast(:ts as jsonb), :kind, :project, :title)"""),
            {
                "id": sid,
                "owner": owner_actor_id,
                "env": environment_id,
                "sb": sandbox_id,
                "root": project_root,
                "ts": _j(thread_settings),
                "kind": kind,
                "project": project_id,
                "title": title,
            },
        )
        return sid

    async def attach_session_project(
        self,
        session_id: str,
        *,
        project_id: str,
        environment_id: str,
        project_root: str,
    ) -> None:
        await self.session.execute(
            text(
                """update workbench_sessions set project_id = :p, environment_id = :e, project_root = :r,
                   last_active_at = now() where id = :id and project_id is null"""
            ),
            {"p": project_id, "e": environment_id, "r": project_root, "id": session_id},
        )

    async def set_session_kind(self, session_id: str, kind: str) -> None:
        await self.session.execute(
            text(
                "update workbench_sessions set kind = :k, last_active_at = now() where id = :id"
            ),
            {"k": kind, "id": session_id},
        )

    async def set_session_title(self, session_id: str, title: str | None) -> None:
        await self.session.execute(
            text("update workbench_sessions set title = :t where id = :id"),
            {"t": title, "id": session_id},
        )

    async def set_session_title_if_empty(self, session_id: str, title: str) -> None:
        await self.session.execute(
            text(
                "update workbench_sessions set title = :t where id = :id and title is null"
            ),
            {"t": title, "id": session_id},
        )

    async def get_session(
        self,
        session_id: str,
        *,
        owner_actor_id: str | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]:
        sql = "select * from workbench_sessions where id = :id" + (
            " for update" if for_update else ""
        )
        r = await self.session.execute(text(sql), {"id": session_id})
        row = r.mappings().first()
        if row is None or (
            owner_actor_id is not None and str(row["owner_actor_id"]) != owner_actor_id
        ):
            raise NotFound("session not found", session_id=session_id)
        return dict(row)

    async def list_sessions(
        self,
        *,
        owner_actor_id: str,
        project_id: str | None = None,
        without_project: bool = False,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "select * from workbench_sessions where owner_actor_id = :o"
        if project_id is not None:
            sql += " and project_id = :p"
        if without_project:
            sql += " and project_id is null"
        if not include_archived:
            sql += " and archived_at is null"
        r = await self.session.execute(
            text(sql + " order by last_active_at desc"),
            {"o": owner_actor_id, "p": project_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def archive_session(self, session_id: str, *, archived: bool) -> None:
        """从工作台移除（或找回）一段对话：只改标记，历史与审批日志都留着。"""
        await self.session.execute(
            text(
                "update workbench_sessions set archived_at = "
                + ("now()" if archived else "null")
                + " where id = :id"
            ),
            {"id": session_id},
        )

    async def set_session_thread(self, session_id: str, thread_id: str) -> None:
        await self.session.execute(
            text(
                "update workbench_sessions set thread_id = :t, last_active_at = now() where id = :id"
            ),
            {"t": thread_id, "id": session_id},
        )

    async def touch_session(self, session_id: str) -> None:
        await self.session.execute(
            text("update workbench_sessions set last_active_at = now() where id = :id"),
            {"id": session_id},
        )

    async def cas_session_wheel(
        self,
        session_id: str,
        *,
        expected_version: int,
        wheel: str,
        active_task_id: str | None,
    ) -> int:
        r = await self.session.execute(
            text("""update workbench_sessions set wheel = :w, active_task_id = :t, state_version = state_version + 1, last_active_at = now()
                    where id = :id and state_version = :v returning state_version"""),
            {"w": wheel, "t": active_task_id, "id": session_id, "v": expected_version},
        )
        v = r.scalar_one_or_none()
        if v is None:
            raise StaleStateVersion(
                "session changed concurrently", session_id=session_id
            )
        return int(v)

    # ---------- events ----------
    async def append_event(
        self,
        *,
        session_id: str,
        kind: str,
        event_type: str,
        payload: dict,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        await self.session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:s, 1))"),
            {"s": session_id},
        )
        r = await self.session.execute(
            text(
                "select coalesce(max(cursor), 0) + 1 from workbench_session_events where session_id = :s"
            ),
            {"s": session_id},
        )
        cursor = int(r.scalar_one())
        eid = str(uuid.uuid4())
        await self.session.execute(
            text("""insert into workbench_session_events (id, session_id, cursor, kind, event_type, payload, task_id, attempt_id)
                    values (:id, :s, :c, :k, :t, cast(:p as jsonb), :task, :attempt)"""),
            {
                "id": eid,
                "s": session_id,
                "c": cursor,
                "k": kind,
                "t": event_type,
                "p": _j(payload),
                "task": task_id,
                "attempt": attempt_id,
            },
        )
        event = {
            "id": eid,
            "session_id": session_id,
            "cursor": cursor,
            "kind": kind,
            "type": event_type,
            "payload": payload,
            "task_id": task_id,
            "attempt_id": attempt_id,
        }
        await SqlOutboxRepository().enqueue(
            self.session,
            OutboxEvent(
                topic="workbench.session_event",
                aggregate_key=session_id,
                deduplication_key=f"evt:{eid}",
                payload=event,
            ),
        )
        return event

    async def append_event_once(
        self,
        *,
        session_id: str,
        kind: str,
        event_type: str,
        payload: dict,
        same: dict[str, Any],
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any] | None:
        """这段对话里还没有同样的事件才记。「同样」= 种类相同，而且内容里含有 same 的各项。

        先拿这段对话的锁再查：两处同时来记，后到的看得见先到的。已经有了就返回 None。
        """
        await self.session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:s, 1))"),
            {"s": session_id},
        )
        r = await self.session.execute(
            text("""select 1 from workbench_session_events
                    where session_id = :s and event_type = :t
                      and payload @> cast(:same as jsonb) limit 1"""),
            {"s": session_id, "t": event_type, "same": _j(same)},
        )
        if r.first() is not None:
            return None
        return await self.append_event(
            session_id=session_id,
            kind=kind,
            event_type=event_type,
            payload=payload,
            task_id=task_id,
            attempt_id=attempt_id,
        )

    async def list_events(
        self, *, session_id: str, after_cursor: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text("""select id, session_id, cursor, kind, event_type as type, payload, task_id, attempt_id, created_at
                    from workbench_session_events where session_id = :s and cursor > :c order by cursor asc limit :l"""),
            {"s": session_id, "c": after_cursor, "l": limit},
        )
        return [_event(m) for m in r.mappings().all()]

    async def conversation_digests(
        self, *, project_id: str, owner_actor_id: str
    ) -> list[dict[str, Any]]:
        """一个项目里的各段对话：几轮、第一句话。给专家挑着读。"""
        r = await self.session.execute(
            text("""select s.id, s.kind, s.title, s.created_at, s.last_active_at,
                           (select count(*) from workbench_session_events e
                             where e.session_id = s.id
                               and e.event_type = 'turn/completed') as turns,
                           (select e.payload->>'text' from workbench_session_events e
                             where e.session_id = s.id
                               and e.event_type = 'turn/requested'
                               and coalesce(e.payload->>'by', 'user') = 'user'
                             order by e.cursor limit 1) as first_message
                    from workbench_sessions s
                    where s.project_id = :p and s.owner_actor_id = :o
                    order by s.created_at, s.id"""),
            {"p": project_id, "o": owner_actor_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def list_record_events(
        self, *, session_id: str, types: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """一段对话里指定种类的全部事件，按先后。不分页：分页在记录上做，不在事件上做。"""
        r = await self.session.execute(
            text("""select id, session_id, cursor, kind, event_type as type, payload, task_id, attempt_id, created_at
                    from workbench_session_events
                    where session_id = :s and event_type = any(:types) order by cursor asc"""),
            {"s": session_id, "types": list(types)},
        )
        return [_event(m) for m in r.mappings().all()]

    async def list_task_events(
        self, task_id: str, *, types: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """一个委托名下、指定种类的事件，按先后。步骤视图用：每一次验收的结果记在这里。"""
        r = await self.session.execute(
            text("""select id, session_id, cursor, kind, event_type as type, payload, task_id, attempt_id, created_at
                    from workbench_session_events
                    where task_id = :t and event_type = any(:types) order by cursor asc"""),
            {"t": task_id, "types": list(types)},
        )
        return [_event(m) for m in r.mappings().all()]

    async def summarize_missing_data(self, *, limit: int) -> list[dict[str, Any]]:
        """各用户查不到数据的公司，按代码汇总：几次、几个人、最近一次、被查的数据集。

        管理端「缺数据的需求」用（账 56）。只汇总，不列出是谁查的。
        """
        r = await self.session.execute(
            text("""select e.payload->>'security_code' as security_code,
                           max(e.payload->>'market') as market,
                           count(*)::int as times,
                           count(distinct s.owner_actor_id)::int as users,
                           max(e.created_at) as last_at,
                           array_remove(array_agg(distinct e.payload->>'dataset'), null) as datasets
                    from workbench_session_events e
                    join workbench_sessions s on s.id = e.session_id
                    where e.event_type = 'data.missing'
                      and e.payload->>'security_code' is not null
                    group by e.payload->>'security_code'
                    order by max(e.created_at) desc, e.payload->>'security_code'
                    limit :limit"""),
            {"limit": limit},
        )
        return [
            {**dict(m), "datasets": sorted(m["datasets"] or [])}
            for m in r.mappings().all()
        ]

    async def list_turn_items(
        self, *, session_id: str, turn_ids: list[str]
    ) -> list[dict[str, Any]]:
        """这几轮里做完的动作（查询、命令）。Codex 发来的事件不带委托编号，按轮次找。"""
        if not turn_ids:
            return []
        r = await self.session.execute(
            text("""select id, session_id, cursor, kind, event_type as type, payload, task_id, attempt_id, created_at
                    from workbench_session_events
                    where session_id = :s and event_type = 'item/completed'
                      and payload->>'turnId' = any(:turns)
                      and payload->'item'->>'type' in ('mcpToolCall', 'commandExecution')
                    order by cursor asc"""),
            {"s": session_id, "turns": list(turn_ids)},
        )
        return [_event(m) for m in r.mappings().all()]

    # ---------- tasks ----------
    async def find_task_by_idempotency(
        self, *, tenant: str, owner_actor_id: str, profile_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select * from workbench_tasks where tenant = :t and owner_actor_id = :o and profile_id = :p and idempotency_key = :k"""
            ),
            {"t": tenant, "o": owner_actor_id, "p": profile_id, "k": idempotency_key},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def insert_task(self, task: dict[str, Any]) -> None:
        cols = list(task.keys())
        json_cols = {
            "original_input",
            "acceptance_contract",
            "execution_policy",
            "budget",
            "rejection",
            "pack_snapshot",
        }
        values = ", ".join(
            f"cast(:{c} as jsonb)" if c in json_cols else f":{c}" for c in cols
        )
        params = {
            c: (_j(task[c]) if c in json_cols and task[c] is not None else task[c])
            for c in cols
        }
        try:
            async with self.session.begin_nested():
                await self.session.execute(
                    text(
                        f"insert into workbench_tasks ({', '.join(cols)}) values ({values})"
                    ),
                    params,
                )
        except IntegrityError as exc:
            if "uq_workbench_tasks_project_active" in str(exc.orig):
                raise ProjectBusy(
                    "the expert is already working on something in this project"
                ) from None
            raise

    async def get_task(
        self,
        task_id: str,
        *,
        owner_actor_id: str | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]:
        sql = "select * from workbench_tasks where id = :id" + (
            " for update" if for_update else ""
        )
        r = await self.session.execute(text(sql), {"id": task_id})
        row = r.mappings().first()
        if row is None or (
            owner_actor_id is not None and str(row["owner_actor_id"]) != owner_actor_id
        ):
            raise NotFound("task not found", task_id=task_id)
        return dict(row)

    async def list_tasks(
        self,
        *,
        owner_actor_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "select * from workbench_tasks where owner_actor_id = :o"
            + (" and session_id = :s" if session_id else "")
            + (" and project_id = :p" if project_id else "")
            + " order by created_at desc"
        )
        r = await self.session.execute(
            text(sql), {"o": owner_actor_id, "s": session_id, "p": project_id}
        )
        return [dict(m) for m in r.mappings().all()]

    async def active_task_of_project(self, project_id: str) -> dict[str, Any] | None:
        """这个项目里专家还在做的那件事；没有就是 None。"""
        r = await self.session.execute(
            text(
                f"""select * from workbench_tasks a where project_id = :p and {_OCCUPYING}
                   order by created_at limit 1"""
            ),
            {"p": project_id, "hold": TURN_HOLD_SECONDS},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def next_queued_task(self, project_id: str) -> dict[str, Any] | None:
        """这个项目里排在后面还没开始的专家委托，最早的那一件（0017：同一项目一次一位）。"""
        r = await self.session.execute(
            text(
                """select * from workbench_tasks where project_id = :p and state = 'RECEIVED'
                   and coalesce(original_input->'client_context'->>'queued_behind', '') <> ''
                   order by created_at limit 1 for update skip locked"""
            ),
            {"p": project_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def queue_environment_recovery(
        self, environment_id: str, *, owner_actor_id: str
    ) -> int:
        rows = await self.session.execute(
            text(
                """select t.id, t.session_id, s.sandbox_id
                   from workbench_tasks t join workbench_sessions s on s.id = t.session_id
                   where s.environment_id = :env and s.owner_actor_id = :owner
                     and t.owner_actor_id = :owner and s.active_task_id = t.id
                     and s.wheel = 'advisor' and t.state = 'WAITING'
                     and t.waiting_reason = 'ENVIRONMENT' and t.cancel_requested_at is null
                     and t.active_attempt_id is null and t.active_interaction_id is null
                   order by t.id for update of t"""
            ),
            {"env": environment_id, "owner": owner_actor_id},
        )
        queued = 0
        for row in rows.mappings().all():
            # task 锁串行化同一任务的两个对账者；第二条语句会看到先提交的命令。
            present = await self.session.execute(
                text(
                    """select 1 from workbench_commands where session_id = :session
                       and kind = 'task.reconnect' and status in ('pending', 'claimed')
                       and payload->>'task_id' = :task limit 1"""
                ),
                {"session": row["session_id"], "task": str(row["id"])},
            )
            if present.scalar_one_or_none() is not None:
                continue
            await self.enqueue_command(
                session_id=str(row["session_id"]),
                sandbox_id=str(row["sandbox_id"]),
                kind="task.reconnect",
                payload={"task_id": str(row["id"]), "environment_id": environment_id},
            )
            queued += 1
        return queued

    async def list_nonterminal_tasks(self) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select * from workbench_tasks where state not in ('SUCCEEDED','REJECTED','FAILED','CANCELLED') order by created_at"
            )
        )
        return [dict(m) for m in r.mappings().all()]

    async def cas_task(
        self, task_id: str, *, expected_version: int, **fields: Any
    ) -> int:
        """比较交换更新 Task 的若干列；返回新 state_version。"""
        json_cols = {
            "budget",
            "acceptance_contract",
            "execution_policy",
            "rejection",
            "original_input",
        }
        sets = ", ".join(
            f"{k} = cast(:{k} as jsonb)" if k in json_cols else f"{k} = :{k}"
            for k in fields
        )
        params = {
            k: (_j(v) if k in json_cols and v is not None else v)
            for k, v in fields.items()
        }
        params.update({"id": task_id, "v": expected_version})
        r = await self.session.execute(
            text(
                f"update workbench_tasks set {sets}, state_version = state_version + 1, updated_at = now() where id = :id and state_version = :v returning state_version"
            ),
            params,
        )
        v = r.scalar_one_or_none()
        if v is None:
            raise StaleStateVersion("task changed concurrently", task_id=task_id)
        return int(v)

    # ---------- attempts ----------
    async def insert_attempt(self, attempt: dict[str, Any]) -> None:
        json_cols = {
            "input_artifact_versions",
            "turn_ids",
            "budget_allocated",
            "budget_consumed",
            "output_artifacts",
            "refs",
        }
        cols = list(attempt.keys())
        values = ", ".join(
            f"cast(:{c} as jsonb)" if c in json_cols else f":{c}" for c in cols
        )
        params = {c: (_j(attempt[c]) if c in json_cols else attempt[c]) for c in cols}
        await self.session.execute(
            text(
                f"insert into workbench_attempts ({', '.join(cols)}) values ({values})"
            ),
            params,
        )

    async def get_attempt(
        self, attempt_id: str, *, for_update: bool = False
    ) -> dict[str, Any]:
        r = await self.session.execute(
            text(
                "select * from workbench_attempts where id = :id"
                + (" for update" if for_update else "")
            ),
            {"id": attempt_id},
        )
        row = r.mappings().first()
        if row is None:
            raise NotFound("attempt not found", attempt_id=attempt_id)
        return dict(row)

    async def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        json_cols = {
            "input_artifact_versions",
            "turn_ids",
            "budget_allocated",
            "budget_consumed",
            "output_artifacts",
            "refs",
        }
        sets = ", ".join(
            f"{k} = cast(:{k} as jsonb)" if k in json_cols else f"{k} = :{k}"
            for k in fields
        )
        params = {k: (_j(v) if k in json_cols else v) for k, v in fields.items()}
        params["id"] = attempt_id
        await self.session.execute(
            text(
                f"update workbench_attempts set {sets}, updated_at = now() where id = :id"
            ),
            params,
        )

    async def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select * from workbench_attempts where task_id = :t order by created_at"
            ),
            {"t": task_id},
        )
        return [dict(m) for m in r.mappings().all()]

    # ---------- interactions ----------
    async def insert_interaction(
        self,
        *,
        session_id: str,
        task_id: str | None,
        attempt_id: str | None,
        kind: str,
        prompt: dict,
        subject_digest: str | None,
        token: str,
        target_state_version: int | None,
        expires_at,
    ) -> str:
        iid = str(uuid.uuid4())
        await self.session.execute(
            text("""insert into workbench_interactions (id, session_id, task_id, attempt_id, kind, prompt, subject_digest, token_hash, target_state_version, expires_at)
                    values (:id, :s, :t, :a, :k, cast(:p as jsonb), :d, :h, :v, :e)"""),
            {
                "id": iid,
                "s": session_id,
                "t": task_id,
                "a": attempt_id,
                "k": kind,
                "p": _j(prompt),
                "d": subject_digest,
                "h": token_hash(token),
                "v": target_state_version,
                "e": expires_at,
            },
        )
        return iid

    async def get_interaction(
        self, interaction_id: str, *, for_update: bool = False
    ) -> dict[str, Any]:
        r = await self.session.execute(
            text(
                "select * from workbench_interactions where id = :id"
                + (" for update" if for_update else "")
            ),
            {"id": interaction_id},
        )
        row = r.mappings().first()
        if row is None:
            raise NotFound("interaction not found", interaction_id=interaction_id)
        return dict(row)

    async def list_interactions(
        self, *, session_id: str, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        sql = (
            "select * from workbench_interactions where session_id = :s"
            + (" and status = :st" if status else "")
            + " order by created_at"
        )
        r = await self.session.execute(text(sql), {"s": session_id, "st": status})
        return [dict(m) for m in r.mappings().all()]

    async def list_owner_interactions(
        self, *, owner_actor_id: str, status: str | None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """这个用户各段对话里的待办，新的在前。归属按对话的所有者认。"""
        sql = (
            """select i.* from workbench_interactions i
               join workbench_sessions s on s.id = i.session_id
               where s.owner_actor_id = :o"""
            + (" and i.status = :st" if status else "")
            + (
                " and (i.expires_at is null or i.expires_at > now())"
                if status == "pending"
                else ""
            )
            + " order by i.created_at desc limit :l"
        )
        r = await self.session.execute(
            text(sql), {"o": owner_actor_id, "st": status, "l": limit}
        )
        return [dict(m) for m in r.mappings().all()]

    async def interaction_opened_cursor(
        self, *, session_id: str, interaction_id: str
    ) -> int | None:
        r = await self.session.execute(
            text("""select cursor from workbench_session_events
                    where session_id = :s and event_type = 'interaction/opened'
                      and payload->>'interaction_id' = :i order by cursor limit 1"""),
            {"s": session_id, "i": interaction_id},
        )
        found = r.scalar_one_or_none()
        return None if found is None else int(found)

    async def consume_interaction(
        self, interaction_id: str, *, response: dict, responded_by: str
    ) -> bool:
        """原子消费：只有 pending 且未过期的那一行会被改；返回是否消费成功。"""
        r = await self.session.execute(
            text("""update workbench_interactions set status = 'consumed', response = cast(:r as jsonb), consumed_at = now(), responded_by = :by
                    where id = :id and status = 'pending' and (expires_at is null or expires_at > now()) returning id"""),
            {"r": _j(response), "by": responded_by, "id": interaction_id},
        )
        return r.scalar_one_or_none() is not None

    async def cancel_pending_interactions(self, *, task_id: str, reason: str) -> int:
        r = await self.session.execute(
            text("""update workbench_interactions set status = 'cancelled', response = cast(:r as jsonb), consumed_at = now()
                    where task_id = :t and status = 'pending' returning id"""),
            {"r": _j({"cancelled": reason}), "t": task_id},
        )
        return len(r.all())

    # ---------- artifacts ----------
    async def put_artifact(
        self,
        *,
        task_id: str,
        attempt_id: str | None,
        name: str,
        kind: str,
        content: Any,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        body = _j(content)
        digest = hashlib.sha256(body.encode()).hexdigest()
        await self.session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:k, 2))"),
            {"k": f"{task_id}:{name}"},
        )
        r = await self.session.execute(
            text(
                "select coalesce(max(version), 0) + 1 from workbench_artifacts where task_id = :t and name = :n"
            ),
            {"t": task_id, "n": name},
        )
        version = int(r.scalar_one())
        aid = str(uuid.uuid4())
        await self.session.execute(
            text("""insert into workbench_artifacts (id, task_id, attempt_id, name, version, kind, content, digest, workspace_path)
                    values (:id, :t, :a, :n, :v, :k, cast(:c as jsonb), :d, :w)"""),
            {
                "id": aid,
                "t": task_id,
                "a": attempt_id,
                "n": name,
                "v": version,
                "k": kind,
                "c": body,
                "d": digest,
                "w": workspace_path,
            },
        )
        return {
            "id": aid,
            "task_id": task_id,
            "name": name,
            "version": version,
            "kind": kind,
            "digest": digest,
        }

    async def get_artifact(
        self, *, task_id: str, name: str, version: int | None = None
    ) -> dict[str, Any] | None:
        sql = "select * from workbench_artifacts where task_id = :t and name = :n" + (
            " and version = :v" if version else " order by version desc limit 1"
        )
        r = await self.session.execute(
            text(sql), {"t": task_id, "n": name, "v": version}
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select id, task_id, attempt_id, name, version, kind, digest, workspace_path, created_at from workbench_artifacts where task_id = :t order by name, version"
            ),
            {"t": task_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def artifact_contents(self, task_id: str) -> dict[str, Any]:
        """一个委托的全部交回物的内容，按交回物的编号取。每个版本都在，不只最新的。"""
        r = await self.session.execute(
            text("select id, content from workbench_artifacts where task_id = :t"),
            {"t": task_id},
        )
        return {str(m["id"]): m["content"] for m in r.mappings().all()}

    async def list_artifacts_with_content(self, task_id: str) -> list[dict[str, Any]]:
        """底稿页用：每个名字取最新版本，带内容。"""
        r = await self.session.execute(
            text(
                """select distinct on (name) id, task_id, attempt_id, name, version, kind, content, digest, workspace_path, created_at
                   from workbench_artifacts where task_id = :t order by name, version desc"""
            ),
            {"t": task_id},
        )
        return [dict(m) for m in r.mappings().all()]

    # ---------- 请来的分身（SDD 0027 P4）----------
    async def pin_knowbot(
        self,
        *,
        owner_actor_id: str,
        owner_subject: str,
        knowbot_id: str,
        version: str,
        digest: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        await self.session.execute(
            text(
                """insert into workbench_knowbot_pins
                   (owner_actor_id, owner_subject, knowbot_id, version, digest, manifest)
                   values (:o, :s, :k, :v, :d, cast(:m as jsonb))
                   on conflict (owner_actor_id, knowbot_id, version) do nothing"""
            ),
            {
                "o": owner_actor_id,
                "s": owner_subject,
                "k": knowbot_id,
                "v": version,
                "d": digest,
                "m": _j(manifest),
            },
        )
        pin = await self.get_knowbot_pin(
            owner_actor_id=owner_actor_id, knowbot_id=knowbot_id, version=version
        )
        assert pin is not None
        if pin["digest"] != digest:
            raise KnowbotInvalid(
                "this knowbot version changed after it was pinned", knowbot=knowbot_id
            )
        return pin

    async def get_knowbot_pin(
        self, *, owner_actor_id: str, knowbot_id: str, version: str
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select * from workbench_knowbot_pins
                   where owner_actor_id = :o and knowbot_id = :k and version = :v"""
            ),
            {"o": owner_actor_id, "k": knowbot_id, "v": version},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def list_knowbot_pins(self, *, owner_actor_id: str) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """select * from workbench_knowbot_pins where owner_actor_id = :o
                   order by created_at desc, knowbot_id, version"""
            ),
            {"o": owner_actor_id},
        )
        return [dict(m) for m in r.mappings().all()]

    # ---------- 作为运行方跑分身测试（SDD 0027 §3.4）----------
    async def actor_for_subject(self, subject: str) -> str | None:
        r = await self.session.execute(
            text(
                "select id from auth_user where subject = :s order by created_at limit 1"
            ),
            {"s": subject},
        )
        found = r.scalar_one_or_none()
        return None if found is None else str(found)

    async def interaction_token(
        self, *, session_id: str, interaction_id: str
    ) -> str | None:
        r = await self.session.execute(
            text("""select payload->>'token' from workbench_session_events
                    where session_id = :s and event_type = 'interaction/opened'
                      and payload->>'interaction_id' = :i order by cursor limit 1"""),
            {"s": session_id, "i": interaction_id},
        )
        return r.scalar_one_or_none()

    async def insert_test_run(
        self, run: dict[str, Any], cases: list[dict[str, Any]]
    ) -> None:
        cols = list(run)
        await self.session.execute(
            text(
                f"insert into workbench_test_runs ({', '.join(cols)}) values ("
                + ", ".join(
                    f"cast(:{c} as jsonb)" if c == "request" else f":{c}" for c in cols
                )
                + ")"
            ),
            {c: (_j(run[c]) if c == "request" else run[c]) for c in cols},
        )
        for case in cases:
            await self.session.execute(
                text(
                    """insert into workbench_test_cases (run_id, case_id, position)
                       values (:r, :c, :p)"""
                ),
                {"r": run["run_id"], "c": case["case_id"], "p": case["position"]},
            )

    async def get_test_run(
        self, run_id: str, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                "select * from workbench_test_runs where run_id = :r"
                + (" for update" if for_update else "")
            ),
            {"r": run_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def list_test_runs(
        self, *, statuses: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """select * from workbench_test_runs where status = any(:s)
                   order by created_at"""
            ),
            {"s": list(statuses)},
        )
        return [dict(m) for m in r.mappings().all()]

    async def update_test_run(self, run_id: str, **changes: Any) -> None:
        allowed = {
            "status",
            "spent",
            "waited_seconds",
            "error",
            "started_at",
            "finished_at",
            "cleanup_after",
        }
        assert set(changes) <= allowed, sorted(set(changes) - allowed)
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        await self.session.execute(
            text(f"update workbench_test_runs set {sets} where run_id = :run_id"),
            {**changes, "run_id": run_id},
        )

    async def test_run_for_task(self, task_id: str) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select r.* from workbench_test_runs r
                   join workbench_test_cases c on c.run_id = r.run_id
                   where c.task_id = :t"""
            ),
            {"t": task_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def list_test_cases(self, run_id: str) -> list[dict[str, Any]]:
        # 不带 outputs：每条最多 2 MB，推进器每 5 秒、knowlab 每次刷新都走这里（审读 10-19）
        r = await self.session.execute(
            text(
                """select run_id, case_id, position, project_id, session_id, task_id,
                          result, waiting_since, waited_seconds
                   from workbench_test_cases where run_id = :r order by position"""
            ),
            {"r": run_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def get_test_case_outputs(
        self, run_id: str, case_id: str
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select outputs from workbench_test_cases
                   where run_id = :r and case_id = :c"""
            ),
            {"r": run_id, "c": case_id},
        )
        return r.scalar_one_or_none()

    async def update_test_case(self, run_id: str, case_id: str, **changes: Any) -> None:
        allowed = {
            "project_id",
            "session_id",
            "task_id",
            "result",
            "outputs",
            "waiting_since",
            "waited_seconds",
        }
        assert set(changes) <= allowed, sorted(set(changes) - allowed)
        json_columns = ("result", "outputs")
        sets = ", ".join(
            f"{k} = cast(:{k} as jsonb)" if k in json_columns else f"{k} = :{k}"
            for k in changes
        )
        params = {
            k: (_j(v) if k in json_columns and v is not None else v)
            for k, v in changes.items()
        }
        await self.session.execute(
            text(
                f"update workbench_test_cases set {sets}"
                " where run_id = :run_id and case_id = :case_id"
            ),
            {**params, "run_id": run_id, "case_id": case_id},
        )

    async def get_test_answer(
        self, run_id: str, question_id: str
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select * from workbench_test_answers
                   where run_id = :r and question_id = :q"""
            ),
            {"r": run_id, "q": question_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def insert_test_answer(
        self, run_id: str, question_id: str, answer: dict[str, Any], answered_by: str
    ) -> None:
        await self.session.execute(
            text(
                """insert into workbench_test_answers
                   (run_id, question_id, answer, answered_by)
                   values (:r, :q, cast(:a as jsonb), :b)"""
            ),
            {"r": run_id, "q": question_id, "a": _j(answer), "b": answered_by},
        )

    async def committed_test_spend(self, since: Any) -> Decimal:
        r = await self.session.execute(
            text(
                """select coalesce(sum(case when status in ('accepted','running','waiting')
                                           then budget_limit else spent end), 0)
                   from workbench_test_runs where created_at >= :since"""
            ),
            {"since": since},
        )
        return Decimal(str(r.scalar_one()))

    async def expired_test_runs(self, now: Any) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """select * from workbench_test_runs
                   where cleanup_after is not null and cleanup_after <= :n"""
            ),
            {"n": now},
        )
        return [dict(m) for m in r.mappings().all()]

    async def delete_session_tree(self, session_id: str) -> None:
        """删一段对话与它下面的一切（只给测试产出到期清理用）。

        交回物、尝试、账、交互随委托级联；事件、交互随对话级联；其余按 session_id 删。
        """
        params = {"s": session_id}
        await self.session.execute(
            text("update workbench_sessions set active_task_id = null where id = :s"),
            params,
        )
        for table in (
            "workbench_approval_log",
            "workbench_tool_grants",
            "workbench_commands",
            "workbench_tasks",
        ):
            await self.session.execute(
                text(f"delete from {table} where session_id = :s"), params
            )
        await self.session.execute(
            text("delete from workbench_sessions where id = :s"), params
        )

    async def clear_test_outputs(self, finished_before: Any) -> None:
        await self.session.execute(
            text(
                """update workbench_test_cases set outputs = null
                   where outputs is not null and run_id in (
                     select run_id from workbench_test_runs
                     where finished_at is not null and finished_at < :b)"""
            ),
            {"b": finished_before},
        )

    async def purge_test_run(self, run_id: str) -> None:
        sessions = [
            str(s)
            for s in (
                await self.session.execute(
                    text(
                        """select session_id from workbench_test_cases
                           where run_id = :r and session_id is not null"""
                    ),
                    {"r": run_id},
                )
            ).scalars()
        ]
        for session_id in sessions:
            await self.delete_session_tree(session_id)
        # 每次测试一个项目（账 129）：到期一起收起，测试账号的项目列表不越积越长
        await self.session.execute(
            text(
                """update workbench_projects set archived_at = now()
                   where archived_at is null and id in (
                     select project_id from workbench_test_cases
                     where run_id = :r and project_id is not null)"""
            ),
            {"r": run_id},
        )
        await self.session.execute(
            text(
                """update workbench_test_cases set session_id = null, task_id = null
                   where run_id = :r"""
            ),
            {"r": run_id},
        )
        await self.session.execute(
            text(
                "update workbench_test_runs set cleanup_after = null where run_id = :r"
            ),
            {"r": run_id},
        )

    # ---------- 知识库（SDD 0011）----------
    async def list_owner_step_artifacts(
        self, *, owner_actor_id: str
    ) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """select a.id, a.task_id, a.name, a.version, a.kind, a.digest, a.created_at,
                          length(a.content::text) as size_bytes,
                          t.project_id, t.session_id, t.profile_id, t.profile_version, t.state,
                          t.original_input, t.created_at as task_created_at,
                          t.pack_source, t.pack_snapshot->>'name' as pack_name
                   from workbench_artifacts a join workbench_tasks t on t.id = a.task_id
                   where t.owner_actor_id = :o and a.kind in ('step', 'user_draft')
                   order by a.created_at"""
            ),
            {"o": owner_actor_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def library_overlay(
        self, *, owner_actor_id: str
    ) -> dict[str, dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select item_id, title, deleted_at from workbench_library_items where owner_actor_id = :o"
            ),
            {"o": owner_actor_id},
        )
        return {m["item_id"]: dict(m) for m in r.mappings().all()}

    async def put_library_overlay(
        self,
        *,
        owner_actor_id: str,
        item_id: str,
        title: str | None = None,
        deleted: bool | None = None,
    ) -> None:
        await self.session.execute(
            text(
                """insert into workbench_library_items (owner_actor_id, item_id, title, deleted_at)
                   values (:o, :i, :t, case when :d then now() else null end)
                   on conflict (owner_actor_id, item_id) do update set
                     title = coalesce(excluded.title, workbench_library_items.title),
                     deleted_at = case when cast(:d_set as boolean) then excluded.deleted_at
                                       else workbench_library_items.deleted_at end,
                     updated_at = now()"""
            ),
            {
                "o": owner_actor_id,
                "i": item_id,
                "t": title,
                "d": bool(deleted),
                "d_set": deleted is not None,
            },
        )

    # ---------- user prefs / credentials（设置面） ----------
    async def get_prefs(self, owner_actor_id: str) -> dict[str, Any]:
        r = await self.session.execute(
            text(
                "select model, approval_policy, mcp_sources, updated_at from workbench_user_prefs where owner_actor_id = :o"
            ),
            {"o": owner_actor_id},
        )
        row = r.mappings().first()
        if row is None:
            return {
                "model": None,
                "approval_policy": "on-request",
                "mcp_sources": None,
                "updated_at": None,
            }
        return dict(row)

    # ---- 长期记忆（0019 §四）----
    async def list_memories(
        self, owner_actor_id: str, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        sql = (
            "select id, kind, text, source, enabled, created_at, updated_at from workbench_memories "
            "where owner_actor_id = :o"
            + (" and enabled" if enabled_only else "")
            + " order by created_at, id"
        )
        r = await self.session.execute(text(sql), {"o": owner_actor_id})
        return [{**dict(m), "id": str(m["id"])} for m in r.mappings().all()]

    async def insert_memory(
        self, *, owner_actor_id: str, kind: str, text: str, source: dict | None
    ) -> dict[str, Any]:
        r = await self.session.execute(
            sa_text(
                """insert into workbench_memories (owner_actor_id, kind, text, source)
                   values (:o, :k, :t, cast(:s as jsonb))
                   returning id, kind, text, source, enabled, created_at, updated_at"""
            ),
            {
                "o": owner_actor_id,
                "k": kind,
                "t": text,
                "s": _j(source) if source is not None else None,
            },
        )
        m = r.mappings().one()
        return {**dict(m), "id": str(m["id"])}

    async def update_memory(
        self,
        memory_id: str,
        *,
        owner_actor_id: str,
        text: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        sets, params = ["updated_at = now()"], {"i": memory_id, "o": owner_actor_id}
        if text is not None:
            sets.append("text = :t")
            params["t"] = text
        if enabled is not None:
            sets.append("enabled = :e")
            params["e"] = enabled
        r = await self.session.execute(
            sa_text(
                f"update workbench_memories set {', '.join(sets)} where id = :i and owner_actor_id = :o "
                "returning id, kind, text, source, enabled, created_at, updated_at"
            ),
            params,
        )
        m = r.mappings().first()
        return {**dict(m), "id": str(m["id"])} if m else None

    async def delete_memory(self, memory_id: str, *, owner_actor_id: str) -> bool:
        r = await self.session.execute(
            sa_text(
                "delete from workbench_memories where id = :i and owner_actor_id = :o"
            ),
            {"i": memory_id, "o": owner_actor_id},
        )
        return (r.rowcount or 0) > 0

    async def clear_memories(self, owner_actor_id: str) -> int:
        r = await self.session.execute(
            sa_text("delete from workbench_memories where owner_actor_id = :o"),
            {"o": owner_actor_id},
        )
        return int(r.rowcount or 0)

    async def put_prefs(
        self,
        owner_actor_id: str,
        *,
        model: str | None,
        approval_policy: str,
        mcp_sources: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        await self.session.execute(
            text(
                """insert into workbench_user_prefs (owner_actor_id, model, approval_policy, mcp_sources, updated_at)
                   values (:o, :m, :a, cast(:s as jsonb), now())
                   on conflict (owner_actor_id) do update set model = excluded.model,
                     approval_policy = excluded.approval_policy,
                     mcp_sources = excluded.mcp_sources, updated_at = now()"""
            ),
            {
                "o": owner_actor_id,
                "m": model,
                "a": approval_policy,
                "s": json.dumps(mcp_sources) if mcp_sources is not None else None,
            },
        )
        return await self.get_prefs(owner_actor_id)

    async def add_credential(
        self,
        *,
        owner_actor_id: str,
        sandbox_id: str | None,
        provider: str,
        ciphertext: str,
        hint: str,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        cid = str(uuid.uuid4())
        await self.session.execute(
            text(
                """insert into workbench_credentials (id, owner_actor_id, sandbox_id, provider, ciphertext, hint, status, base_url)
                   values (:id, :o, :s, :p, :c, :h, 'active', :u)"""
            ),
            {
                "id": cid,
                "o": owner_actor_id,
                "s": sandbox_id,
                "p": provider,
                "c": ciphertext,
                "h": hint,
                "u": base_url,
            },
        )
        return {
            "id": cid,
            "provider": provider,
            "hint": hint,
            "status": "active",
            "sandbox_id": sandbox_id,
            "base_url": base_url,
        }

    async def list_credentials(self, owner_actor_id: str) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """select id, sandbox_id, provider, hint, status, base_url, created_at, revoked_at
                   from workbench_credentials where owner_actor_id = :o order by created_at desc"""
            ),
            {"o": owner_actor_id},
        )
        return [dict(m) for m in r.mappings().all()]

    async def revoke_credential(
        self, credential_id: str, *, owner_actor_id: str
    ) -> bool:
        r = await self.session.execute(
            text(
                """update workbench_credentials set status = 'revoked', revoked_at = now(), ciphertext = ''
                   where id = :id and owner_actor_id = :o and status = 'active'"""
            ),
            {"id": credential_id, "o": owner_actor_id},
        )
        return int(getattr(r, "rowcount", 0)) == 1

    # ---------- relay identity (0004 动态登记) ----------
    @asynccontextmanager
    async def relay_identity_operation(self, owner_actor_id: str):
        # Separate connection: identity records must commit before external side effects,
        # while the operation lock survives those commits. Transaction end releases the
        # advisory lock even on cancellation; no session lock can leak into the pool.
        engine = self.session.bind
        if not isinstance(engine, AsyncEngine):
            raise RuntimeError("identity operation requires an engine-bound session")
        async with engine.begin() as guard:
            acquired = await guard.scalar(
                text("select pg_try_advisory_xact_lock(hashtextextended(:owner, 43))"),
                {"owner": owner_actor_id},
            )
            if not acquired:
                raise RelayIdentityBusy(
                    "An identity operation is in progress; refresh status"
                )
            yield

    async def create_agent_pairing(
        self, row: dict[str, Any], *, pending_limit: int = 500
    ) -> bool:
        """Reserve global pending capacity and insert without exposing code collisions."""
        await self.session.execute(
            text(
                "select pg_advisory_xact_lock(hashtextextended('agent-pairing-pending-cap', 0))"
            )
        )
        await self.session.execute(
            text("""update workbench_agent_pairings set status='expired', token_ciphertext=null
                   where status in ('pending','approved') and expires_at <= now()""")
        )
        pending = int(
            await self.session.scalar(
                text(
                    "select count(*) from workbench_agent_pairings where status='pending'"
                )
            )
        )
        if pending >= pending_limit:
            return False
        result = await self.session.execute(
            text("""insert into workbench_agent_pairings
                 (id,code_hash,device_secret_hash,machine_name,os,agent_version,codex_version,source_ip,status,expires_at)
                 values (cast(:id as uuid),:code_hash,:device_secret_hash,:machine_name,:os,:agent_version,:codex_version,:source_ip,'pending',:expires_at)
                 on conflict (code_hash) do nothing returning id"""),
            row,
        )
        return result.first() is not None

    async def get_agent_pairing(
        self, pairing_id: str, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                "select * from workbench_agent_pairings where id=cast(:id as uuid)"
                + (" for update" if for_update else "")
            ),
            {"id": pairing_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def get_pending_agent_pairing_by_code(
        self, code_hash: str, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                "select * from workbench_agent_pairings where code_hash=:hash and status='pending' and expires_at>now()"
                + (" for update" if for_update else "")
            ),
            {"hash": code_hash},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def update_agent_pairing(
        self, pairing_id: str, *, expected_status: str, **fields: Any
    ) -> bool:
        allowed = {
            "status",
            "owner_actor_id",
            "relay_user",
            "relay_url",
            "agent_token_expires_at",
            "token_ciphertext",
            "decided_at",
            "delivered_at",
            "attempts",
            "last_polled_at",
        }
        if not fields or set(fields) - allowed:
            raise ValueError("invalid agent pairing update fields")
        assignments = ",".join(f"{name}=:{name}" for name in fields)
        values = dict(fields) | {"id": pairing_id, "expected_status": expected_status}
        result = await self.session.execute(
            text(
                f"update workbench_agent_pairings set {assignments} where id=cast(:id as uuid) and status=:expected_status"
            ),
            values,
        )
        return int(getattr(result, "rowcount", 0)) == 1

    async def mark_agent_pairing_delivered(
        self, pairing_id: str
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text("""with approved as (
                     select id, relay_user, relay_url, agent_token_expires_at,
                            token_ciphertext, owner_actor_id
                     from workbench_agent_pairings
                     where id=cast(:id as uuid) and status='approved'
                       and token_ciphertext is not null
                     for update
                   ), delivered as (
                     update workbench_agent_pairings as pairing
                     set status='delivered', token_ciphertext=null, delivered_at=now()
                     from approved
                     where pairing.id=approved.id and pairing.status='approved'
                     returning approved.relay_user, approved.relay_url,
                               approved.agent_token_expires_at,
                               approved.token_ciphertext, approved.owner_actor_id
                   )
                   select * from delivered"""),
            {"id": pairing_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def current_online_environment(
        self, owner_actor_id: str
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text("""select id,name,last_seen_at from workbench_environments where owner_actor_id=cast(:owner as uuid)
                   and status='online' order by last_seen_at desc nulls last,updated_at desc limit 1"""),
            {"owner": owner_actor_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def append_agent_audit_event(
        self,
        *,
        owner_actor_id: str,
        event_type: str,
        pairing_id: str,
        metadata: dict[str, Any],
    ) -> None:
        await self.session.execute(
            text("""insert into workbench_agent_audit_events (id,owner_actor_id,event_type,pairing_id,metadata)
                   values (cast(:id as uuid),cast(:owner as uuid),:event_type,cast(:pairing as uuid),cast(:metadata as jsonb))"""),
            {
                "id": str(uuid.uuid4()),
                "owner": owner_actor_id,
                "event_type": event_type,
                "pairing": pairing_id,
                "metadata": _j(metadata),
            },
        )

    async def create_agent_install_credential(
        self, *, owner_actor_id: str, token_hash: str, expires_at
    ) -> None:
        await self.session.execute(
            text("""insert into workbench_agent_install_credentials (id,owner_actor_id,token_hash,expires_at)
                   values (cast(:id as uuid),cast(:owner as uuid),:hash,:expires_at)"""),
            {
                "id": str(uuid.uuid4()),
                "owner": owner_actor_id,
                "hash": token_hash,
                "expires_at": expires_at,
            },
        )

    async def consume_agent_install_credential(
        self, token_hash: str, *, now
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text("""update workbench_agent_install_credentials set use_count=use_count+1
                   where token_hash=:hash and expires_at>:now and use_count<5
                   returning owner_actor_id,expires_at,use_count"""),
            {"hash": token_hash, "now": now},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def cleanup_agent_onboarding(self, *, now) -> dict[str, int]:
        expired = await self.session.execute(
            text("""update workbench_agent_pairings set status='expired',token_ciphertext=null
                   where status in ('pending','approved') and expires_at<=:now returning id"""),
            {"now": now},
        )
        credentials = await self.session.execute(
            text(
                "delete from workbench_agent_install_credentials where expires_at<=:now returning id"
            ),
            {"now": now},
        )
        return {
            "pairings": len(expired.all()),
            "install_credentials": len(credentials.all()),
        }

    async def get_relay_identity(self, owner_actor_id: str) -> dict[str, Any] | None:
        r = await self.session.execute(
            text("select * from workbench_relay_identities where owner_actor_id = :o"),
            {"o": owner_actor_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def put_relay_identity(
        self,
        owner_actor_id: str,
        *,
        relay_user: str,
        agent_token_ciphertext: str,
        sandbox_token_ciphertext: str,
    ) -> None:
        await self.session.execute(
            text(
                """insert into workbench_relay_identities (owner_actor_id, relay_user, agent_token_ciphertext, sandbox_token_ciphertext)
                   values (:o, :u, :a, :s)
                   on conflict (owner_actor_id) do update set agent_token_ciphertext = excluded.agent_token_ciphertext,
                     sandbox_token_ciphertext = excluded.sandbox_token_ciphertext"""
            ),
            {
                "o": owner_actor_id,
                "u": relay_user,
                "a": agent_token_ciphertext,
                "s": sandbox_token_ciphertext,
            },
        )

    async def mark_relay_registered(self, owner_actor_id: str) -> None:
        await self.session.execute(
            text(
                "update workbench_relay_identities set registered_at = now() where owner_actor_id = :o"
            ),
            {"o": owner_actor_id},
        )

    async def count_live_provisioned_sandboxes(self) -> int:
        """F-SBX-08：按需拉起、且没回收的沙箱数（全体用户）。"""
        r = await self.session.execute(
            text(
                "select count(*) from workbench_sandboxes where provisioned = true and status <> 'deleted'"
            )
        )
        return int(r.scalar_one())

    async def has_live_provisioned_sandbox(self, owner_actor_id: str) -> bool:
        r = await self.session.execute(
            text(
                "select 1 from workbench_sandboxes where owner_actor_id = :o and provisioned = true and status <> 'deleted' limit 1"
            ),
            {"o": owner_actor_id},
        )
        return r.first() is not None

    async def upsert_provisioned_sandbox(
        self,
        *,
        owner_actor_id: str,
        app_server_url: str,
        token_ref: str,
        codex_version: str | None,
        relay_user: str,
        status: str,
    ) -> str:
        r = await self.session.execute(
            text(
                "select id from workbench_sandboxes where owner_actor_id = :o and provisioned = true order by created_at limit 1"
            ),
            {"o": owner_actor_id},
        )
        row = r.first()
        if row:
            await self.session.execute(
                text(
                    """update workbench_sandboxes set app_server_url = :url, token_ref = :ref, codex_version = :cv,
                       relay_user = :ru, status = :st, updated_at = now() where id = :id"""
                ),
                {
                    "url": app_server_url,
                    "ref": token_ref,
                    "cv": codex_version,
                    "ru": relay_user,
                    "st": status,
                    "id": str(row[0]),
                },
            )
            return str(row[0])
        sb_id = str(uuid.uuid4())
        await self.session.execute(
            text(
                """insert into workbench_sandboxes (id, owner_actor_id, app_server_url, token_ref, codex_version, status, relay_user, provisioned)
                   values (:id, :o, :url, :ref, :cv, :st, :ru, true)"""
            ),
            {
                "id": sb_id,
                "o": owner_actor_id,
                "url": app_server_url,
                "ref": token_ref,
                "cv": codex_version,
                "st": status,
                "ru": relay_user,
            },
        )
        return sb_id

    async def set_sandbox_status(self, sandbox_id: str, status: str) -> None:
        await self.session.execute(
            text(
                "update workbench_sandboxes set status = :s, updated_at = now() where id = :id"
            ),
            {"s": status, "id": sandbox_id},
        )

    async def active_credential(self, owner_actor_id: str) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select id, provider, ciphertext, hint, base_url from workbench_credentials
                   where owner_actor_id = :o and status = 'active' order by created_at desc limit 1"""
            ),
            {"o": owner_actor_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    # ---------- budget ----------
    async def ledger_append(
        self,
        *,
        task_id: str,
        attempt_id: str | None,
        entry: str,
        amount: Decimal,
        tokens: dict | None = None,
        note: str | None = None,
        actor_id: str | None = None,
    ) -> None:
        await self.session.execute(
            text("""insert into workbench_budget_ledger (task_id, attempt_id, entry, amount, tokens, note, actor_id)
                    values (:t, :a, :e, :m, cast(:k as jsonb), :n, :by)"""),
            {
                "t": task_id,
                "a": attempt_id,
                "e": entry,
                "m": amount,
                "k": _j(tokens or {}),
                "n": note,
                "by": actor_id,
            },
        )

    async def ledger_list(self, task_id: str) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                "select * from workbench_budget_ledger where task_id = :t order by created_at, id"
            ),
            {"t": task_id},
        )
        return [dict(m) for m in r.mappings().all()]

    # ---------- approvals ----------
    async def log_approval(
        self,
        *,
        session_id: str,
        task_id: str | None,
        request_id: str,
        method: str,
        summary: dict,
        decision: str,
        source: str,
        interaction_id: str | None = None,
        wheel: str | None = None,
        step_id: str | None = None,
    ) -> None:
        await self.session.execute(
            text("""insert into workbench_approval_log (session_id, task_id, request_id, method, summary, decision, source, interaction_id, wheel, step_id)
                    values (:s, :t, :r, :m, cast(:sm as jsonb), :d, :src, :i, :w, :st)"""),
            {
                "s": session_id,
                "t": task_id,
                "r": request_id,
                "m": method,
                "sm": _j(summary),
                "d": decision,
                "src": source,
                "i": interaction_id,
                "w": wheel,
                "st": step_id,
            },
        )

    # ---------- commands（网页接口 → runner） ----------

    async def list_approvals(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = [], {}
        if session_id is not None:
            where.append("session_id = :s")
            params["s"] = session_id
        if task_id is not None:
            where.append("task_id = :t")
            params["t"] = task_id
        if step_id is not None:
            where.append("step_id = :st")
            params["st"] = step_id
        sql = "select id, session_id, task_id, step_id, wheel, request_id, method, summary, decision, source, interaction_id, created_at from workbench_approval_log"
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by created_at, id"
        r = await self.session.execute(text(sql), params)
        return [
            {
                **dict(m),
                "id": str(m["id"]),
                "session_id": str(m["session_id"]),
                "task_id": str(m["task_id"]) if m["task_id"] else None,
                "interaction_id": str(m["interaction_id"])
                if m["interaction_id"]
                else None,
            }
            for m in r.mappings().all()
        ]

    async def insert_tool_grant(
        self,
        *,
        session_id: str,
        task_id: str | None,
        category: str,
        command_prefix: list[str] | None,
        granted_by: str,
    ) -> str:
        r = await self.session.execute(
            text(
                """insert into workbench_tool_grants (session_id, task_id, category, command_prefix, granted_by)
                   values (:s, :t, :c, cast(:p as jsonb), :g) returning id"""
            ),
            {
                "s": session_id,
                "t": task_id,
                "c": category,
                "p": _j(command_prefix) if command_prefix is not None else None,
                "g": granted_by,
            },
        )
        return str(r.scalar_one())

    async def list_tool_grants(
        self, *, session_id: str, task_id: str | None
    ) -> list[dict[str, Any]]:
        if task_id is not None:
            r = await self.session.execute(
                text(
                    "select id, category, command_prefix, created_at from workbench_tool_grants where task_id = :t order by created_at"
                ),
                {"t": task_id},
            )
        else:
            # 自驾的授权到下一次交接为止：只认最近一次 wheel/handover 之后记下的
            r = await self.session.execute(
                text(
                    """select g.id, g.category, g.command_prefix, g.created_at from workbench_tool_grants g
                       where g.session_id = :s and g.task_id is null
                         and g.created_at > coalesce(
                           (select max(e.created_at) from workbench_session_events e
                             where e.session_id = :s and e.event_type = 'wheel/handover'),
                           'epoch'::timestamptz)
                       order by g.created_at"""
                ),
                {"s": session_id},
            )
        return [{**dict(m), "id": str(m["id"])} for m in r.mappings().all()]

    async def enqueue_command(
        self, *, session_id: str, sandbox_id: str, kind: str, payload: dict
    ) -> str:
        cid = str(uuid.uuid4())
        await self.session.execute(
            text(
                """insert into workbench_commands (id, session_id, sandbox_id, kind, payload)
                   values (:id, :s, :sb, :k, cast(:p as jsonb))"""
            ),
            {"id": cid, "s": session_id, "sb": sandbox_id, "k": kind, "p": _j(payload)},
        )
        return cid

    # ---------- sandbox leases（runner 分片，F-LEDGER-02） ----------
    async def acquire_leases(self, *, runner_id: str, ttl_seconds: int) -> list[str]:
        """给有待办命令、或有委托要人推（排队、在跑、等审批）的沙箱拿租约：没有租约、租约已过期、或本来就是这个名字的才拿得到；返回拿到的沙箱 id。哪些是本进程新拿到的，由 runner 自己记（0034）。"""
        r = await self.session.execute(
            text(
                """with wanted as (
                     select sandbox_id from workbench_commands where status = 'pending'
                     union
                     -- 有委托要人推的沙箱：上一任 runner 死了或同名重启了，没有待办命令也要拿下来对账（0034）
                     select s.sandbox_id from workbench_tasks t join workbench_sessions s on s.id = t.session_id
                     where t.state in ('RECEIVED', 'VALIDATING', 'QUEUED', 'RUNNING', 'WAITING')
                   ), taken as (
                     insert into workbench_sandbox_leases (sandbox_id, runner_id, expires_at, acquired_at, updated_at)
                     select sandbox_id, :r, now() + make_interval(secs => :ttl), now(), now() from wanted
                     on conflict (sandbox_id) do update
                       set runner_id = excluded.runner_id, expires_at = excluded.expires_at,
                           acquired_at = case when workbench_sandbox_leases.runner_id = excluded.runner_id
                                              then workbench_sandbox_leases.acquired_at else now() end,
                           updated_at = now()
                       where workbench_sandbox_leases.runner_id = excluded.runner_id
                          or workbench_sandbox_leases.expires_at < now()
                     returning sandbox_id
                   )
                   select sandbox_id from taken"""
            ),
            {"r": runner_id, "ttl": ttl_seconds},
        )
        return [str(x[0]) for x in r.all()]

    async def sandbox_held(self, sandbox_id: str) -> bool:
        r = await self.session.execute(
            text(
                "select 1 from workbench_sandbox_leases where sandbox_id = :s and expires_at > now()"
            ),
            {"s": sandbox_id},
        )
        return r.first() is not None

    async def renew_leases(self, *, runner_id: str, ttl_seconds: int) -> list[str]:
        """续本 runner 的租约；返回仍归本 runner 的沙箱 id（丢了的就不在里面）。"""
        r = await self.session.execute(
            text(
                """update workbench_sandbox_leases set expires_at = now() + make_interval(secs => :ttl), updated_at = now()
                   where runner_id = :r and expires_at >= now() returning sandbox_id"""
            ),
            {"r": runner_id, "ttl": ttl_seconds},
        )
        return [str(x[0]) for x in r.all()]

    async def release_leases(self, *, runner_id: str) -> None:
        # 到期而不是删除：运行身份对这张表只有 SELECT/INSERT/UPDATE（数据库策略不发 DELETE）
        await self.session.execute(
            text(
                "update workbench_sandbox_leases set expires_at = now(), updated_at = now() where runner_id = :r"
            ),
            {"r": runner_id},
        )

    async def expire_pending_tool_approvals(
        self, *, sandbox_id: str, reason: str
    ) -> list[dict[str, Any]]:
        """接管一个沙箱时：上一个 runner 手里未决的工具审批作废（app-server 那边的请求已随连接消失，Codex 会重发）。"""
        r = await self.session.execute(
            text(
                """update workbench_interactions i set status = 'expired', response = cast(:resp as jsonb), consumed_at = now()
                   from workbench_sessions s
                   where i.session_id = s.id and s.sandbox_id = :sb and i.kind = 'tool_approval' and i.status = 'pending'
                   returning i.id, i.session_id, i.task_id"""
            ),
            {"sb": sandbox_id, "resp": _j({"expired": reason})},
        )
        return [
            {
                "id": str(m["id"]),
                "session_id": str(m["session_id"]),
                "task_id": str(m["task_id"]) if m["task_id"] else None,
            }
            for m in r.mappings().all()
        ]

    async def unsettled_tasks(self, sandbox_ids: list[str]) -> list[dict[str, Any]]:
        if not sandbox_ids:
            return []
        r = await self.session.execute(
            text(
                """select t.id, t.session_id, s.sandbox_id, t.state, t.state_version, t.thread_id,
                          t.active_attempt_id, t.cancel_requested_at,
                          i.prompt->'subject'->>'request_id' as request_id,
                          exists(select 1 from workbench_commands c
                                 where c.kind = 'task.drive' and c.status in ('pending', 'claimed')
                                   and c.payload->>'task_id' = t.id::text) as drive_queued
                   from workbench_tasks t join workbench_sessions s on s.id = t.session_id
                   left join workbench_interactions i on i.id = t.active_interaction_id
                   where s.sandbox_id::text = any(:sbs)
                     and (t.state in ('VALIDATING', 'QUEUED', 'RUNNING')
                          or (t.state = 'RECEIVED'
                              and coalesce(t.original_input->'client_context'->>'queued_behind', '') = '')
                          or (t.state = 'WAITING' and coalesce(i.prompt->'subject'->>'request_id', '') <> ''))
                   order by t.created_at"""
            ),
            {"sbs": list(sandbox_ids)},
        )
        return [
            {
                **dict(m),
                "id": str(m["id"]),
                "session_id": str(m["session_id"]),
                "sandbox_id": str(m["sandbox_id"]),
                "active_attempt_id": (
                    str(m["active_attempt_id"]) if m["active_attempt_id"] else None
                ),
            }
            for m in r.mappings().all()
        ]

    async def projects_with_stalled_queue(self) -> list[str]:
        r = await self.session.execute(
            text(
                f"""select distinct q.project_id from workbench_tasks q
                   where q.project_id is not null and q.state = 'RECEIVED'
                     and coalesce(q.original_input->'client_context'->>'queued_behind', '') <> ''
                     and not exists (
                       select 1 from workbench_tasks a where a.project_id = q.project_id and {_OCCUPYING})"""
            ),
            {"hold": TURN_HOLD_SECONDS},
        )
        return [str(x[0]) for x in r.all()]

    async def expired_turn_holds(self) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """select id, project_id, original_input->'client_context'->'turn_open' as hold
                   from workbench_tasks
                   where state in ('SUCCEEDED', 'REJECTED', 'FAILED', 'CANCELLED')
                     and original_input->'client_context'->'turn_open' is not null
                     and (original_input->'client_context'->'turn_open'->>'since')::timestamptz
                         <= now() - make_interval(secs => :hold)"""
            ),
            {"hold": TURN_HOLD_SECONDS},
        )
        return [
            {"id": str(m["id"]), "project_id": str(m["project_id"]), "hold": m["hold"]}
            for m in r.mappings().all()
        ]

    async def claim_commands(
        self, *, claimed_by: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        r = await self.session.execute(
            text(
                """update workbench_commands set status = 'claimed', claimed_by = :by, claimed_at = now()
                   where id in (
                     select c.id from workbench_commands c
                     join workbench_sandbox_leases l on l.sandbox_id = c.sandbox_id
                     where c.status = 'pending' and l.runner_id = :by and l.expires_at > now()
                     order by c.created_at limit :l for update of c skip locked
                   ) returning *"""
            ),
            {"by": claimed_by, "l": limit},
        )
        out = []
        for m in r.mappings().all():
            d = dict(m)
            for k in ("id", "session_id", "sandbox_id"):
                d[k] = str(d[k])
            out.append(d)
        return out

    async def finish_command(
        self, command_id: str, *, error: str | None = None
    ) -> None:
        await self.session.execute(
            text(
                """update workbench_commands set status = :st, finished_at = now(), error = :e where id = :id"""
            ),
            {"st": "failed" if error else "done", "e": error, "id": command_id},
        )

    async def requeue_stale_commands(self, *, older_than_seconds: int = 300) -> int:
        r = await self.session.execute(
            text(
                """update workbench_commands set status = 'pending', claimed_by = null, claimed_at = null
                   where status = 'claimed' and claimed_at < now() - make_interval(secs => :s) returning id"""
            ),
            {"s": older_than_seconds},
        )
        return len(r.all())

    async def get_session_by_thread(self, thread_id: str) -> dict[str, Any] | None:
        r = await self.session.execute(
            text("select * from workbench_sessions where thread_id = :t"),
            {"t": thread_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    async def get_interaction_by_request(
        self, *, session_id: str, request_id: str
    ) -> dict[str, Any] | None:
        r = await self.session.execute(
            text(
                """select * from workbench_interactions where session_id = :s and kind = 'tool_approval'
                   and prompt->'subject'->>'request_id' = :r order by created_at desc limit 1"""
            ),
            {"s": session_id, "r": request_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None


class SqlWorkbenchStores:
    """端口 `WorkbenchStores` 的实现：每个工作单元一个数据库会话、一个仓储。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sessions = session_factory

    @asynccontextmanager
    async def __call__(self):
        async with self._sessions() as session:
            yield WorkbenchRepository(session)
