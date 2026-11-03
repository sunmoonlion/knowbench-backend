"""找专家请求丢响应、并发重发及受理失败：真实 HTTP 与隔离 PostgreSQL。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from test_workbench_ledger_db import db as db  # noqa: F401
from test_workbench_project_routes import machine, project
from test_workbench_routes import A, B
from test_workbench_routes import make_client as make_client  # noqa: F401

from app.infrastructure.workbench.repository import WorkbenchRepository

BODY = {"question": "核对这份文件里的条款", "idempotency_key": "find-retry-001"}


async def counts(db):
    async with db() as session:
        return tuple(
            [
                (
                    await session.execute(text(f"select count(*) from {table}"))
                ).scalar_one()
                for table in (
                    "workbench_sessions",
                    "workbench_tasks",
                    "workbench_commands",
                    "workbench_session_events",
                )
            ]
        )


async def test_retry_returns_the_original_task_session_and_only_one_command(
    make_client, db
):
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        url = f"/api/workbench/projects/{pid}/find-expert"
        first = await http.post(url, json=BODY)
        assert first.status_code == 201, first.text
        before = await counts(db)
        replay = await http.post(url, json=BODY)
        assert replay.status_code == 201, replay.text
        assert replay.json() == first.json()
        assert await counts(db) == before
        async with db() as session:
            commands = (
                (
                    await session.execute(
                        text("select session_id, kind, payload from workbench_commands")
                    )
                )
                .mappings()
                .all()
            )
        assert len(commands) == 1
        assert commands[0]["kind"] == "task.drive"
        assert str(commands[0]["session_id"]) == first.json()["session_id"]
        assert commands[0]["payload"]["task_id"] == first.json()["task_id"]


async def test_changed_budget_conflicts_without_creating_an_empty_session(
    make_client, db
):
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        url = f"/api/workbench/projects/{pid}/find-expert"
        assert (await http.post(url, json=BODY)).status_code == 201
        before = await counts(db)
        changed = await http.post(url, json=BODY | {"budget_limit": "3"})
        assert changed.status_code == 409
        assert changed.json()["code"] == "idempotency_conflict"
        assert await counts(db) == before


async def test_two_simultaneous_submissions_share_one_find_round(make_client, db):
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        url = f"/api/workbench/projects/{pid}/find-expert"
        one, two = await asyncio.gather(
            http.post(url, json=BODY), http.post(url, json=BODY)
        )
        assert one.status_code == two.status_code == 201, (one.text, two.text)
        assert one.json() == two.json()
        assert (await counts(db))[:3] == (1, 1, 1)


async def test_replaying_a_queued_or_cancelled_round_never_restarts_it(make_client, db):
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        url = f"/api/workbench/projects/{pid}/find-expert"
        first = await http.post(url, json=BODY)
        queued_body = BODY | {"idempotency_key": "find-deliberate-new-round"}
        queued = await http.post(url, json=queued_body)
        assert first.status_code == queued.status_code == 201
        assert first.json()["task_id"] != queued.json()["task_id"]
        before = await counts(db)
        assert before[:3] == (2, 2, 1)  # 排队者不提前派发
        assert (await http.post(url, json=queued_body)).json() == queued.json()
        assert await counts(db) == before
        tid = queued.json()["task_id"]
        assert (
            await http.post(f"/api/workbench/tasks/{tid}/cancel")
        ).status_code == 202
        before = await counts(db)
        # 查回旧请求不要求电脑重新在线，不借重试重新建一笔付费请求。
        async with db() as session:
            await session.execute(
                text("update workbench_environments set status='offline' where id=:id"),
                {"id": env},
            )
            await session.commit()
        assert (await http.post(url, json=queued_body)).json() == queued.json()
        assert await counts(db) == before
        assert (await http.get(f"/api/workbench/find-expert/{tid}")).json()[
            "state"
        ] == "CANCELLED"


@pytest.mark.parametrize("failure", ["offline", "queue"])
async def test_failed_intake_rolls_back_session_task_events_and_command(
    make_client, db, monkeypatch, failure
):
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        url = f"/api/workbench/projects/{pid}/find-expert"
        before = await counts(db)
        if failure == "offline":
            async with db() as session:
                await session.execute(
                    text(
                        "update workbench_environments set status='offline' where id=:id"
                    ),
                    {"id": env},
                )
                await session.commit()
            response = await http.post(url, json=BODY)
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "environment_offline"
        else:
            monkeypatch.setattr(
                WorkbenchRepository,
                "enqueue_command",
                AsyncMock(side_effect=RuntimeError("synthetic queue failure")),
            )
            with pytest.raises(RuntimeError, match="synthetic queue failure"):
                await http.post(url, json=BODY)
        assert await counts(db) == before


async def test_another_owner_cannot_retry_or_read_the_round(make_client, db):
    async with make_client(A) as http:
        env = await machine(http)
        pid = (await project(http, env)).json()["id"]
        url = f"/api/workbench/projects/{pid}/find-expert"
        first = await http.post(url, json=BODY)
        assert first.status_code == 201
    before = await counts(db)
    async with make_client(B) as http:
        await machine(http)
        refused = await http.post(url, json=BODY)
        assert refused.status_code == 404
        hidden = await http.get(f"/api/workbench/find-expert/{first.json()['task_id']}")
        assert hidden.status_code == 404
    assert await counts(db) == before
