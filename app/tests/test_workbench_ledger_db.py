"""账房的数据库级行为（AT-01/02/05/06/07/12/13/14 的载体）。同 test_agent_reliability_db：真迁移链、每个测试独立 schema。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.workbench.ledger import Ledger
from app.application.workbench.session_service import SessionService
from app.domain.workbench.errors import (
    IdempotencyConflict,
    InteractionRejected,
    NotFound,
    RootOutsideWhitelist,
    StaleStateVersion,
    WheelHeldByOther,
)
from app.domain.workbench.models import HandoverRequest, InteractionPrompt
from app.domain.workbench.states import (
    AttemptState,
    InvalidTransition,
    TaskState,
    WaitingReason,
)
from app.infrastructure.storage.postgres import make_session_factory
from app.infrastructure.workbench.repository import WorkbenchRepository

ROOT = Path(__file__).resolve().parents[1]


def migrate(connection):
    with Operations.context(MigrationContext.configure(connection)):
        for path in sorted((ROOT / "alembic/versions").glob("20*.py")):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade()


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set AGENT_TEST_DATABASE_URL to run PostgreSQL workbench tests")
    assert url.rsplit("/", 1)[-1].endswith("_tests")
    schema = "wb_test_" + uuid.uuid4().hex
    sync = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    with sync.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        migrate(connection)
    engine = create_async_engine(
        url.replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()
        with sync.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        sync.dispose()


OWNER = str(uuid.uuid4())
OTHER = str(uuid.uuid4())


async def seed(factory, *, owner=OWNER, root="/home/u/research/proj"):
    async with factory() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            env = await repo.register_environment(
                owner_actor_id=owner,
                name="laptop",
                agent_version="0.1.0",
                codex_version="0.155.1",
                roots=["/home/u/research"],
                ceiling={"sandbox": "workspace-write", "network": False},
            )
            sb = await repo.register_sandbox(
                owner_actor_id=owner,
                app_server_url="ws://sandbox:47800",
                token_ref="secret/x",
                codex_version="0.155.1",
            )
        sid = (
            await SessionService(repo).create(
                owner_actor_id=owner,
                environment_id=env,
                sandbox_id=sb,
                project_root=root,
            )
        )["session_id"]
        return env, sb, sid


def handover(sid, key="k-0000-0001", budget="10", text="核对 2025 年报附注"):
    return HandoverRequest(
        idempotency_key=key,
        session_id=sid,
        profile_id="DATA_QUERY",
        original_input={"text": text},
        budget_limit=Decimal(budget),
    )


async def test_handover_creates_task_and_moves_wheel_atomically(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        r = await Ledger(repo).handover(handover(sid), owner_actor_id=OWNER)
        assert r["created"] and r["state"] == TaskState.RECEIVED
        sess = await repo.get_session(sid)
        assert (
            sess["wheel"] == "advisor" and str(sess["active_task_id"]) == r["task_id"]
        )
        types = [e["type"] for e in await repo.list_events(session_id=sid)]
        # 交给专家 = 这段对话从此是专家对话（0017），所以多一条 kind_changed
        assert types == [
            "session/created",
            "session/kind_changed",
            "task/received",
            "wheel/handover",
        ]
        # 用户在 advisor 期间不能发 turn（AT-06）
        with pytest.raises(WheelHeldByOther):
            await SessionService(repo).assert_user_may_drive(sid, owner_actor_id=OWNER)


async def test_idempotent_handover_same_and_different_digest(db):
    _, _, sid = await seed(db)
    async with db() as s:
        led = Ledger(WorkbenchRepository(s))
        a = await led.handover(handover(sid), owner_actor_id=OWNER)
        b = await led.handover(handover(sid), owner_actor_id=OWNER)
        assert a["task_id"] == b["task_id"] and b["created"] is False  # AT-01
        with pytest.raises(IdempotencyConflict):  # AT-02
            await led.handover(handover(sid, text="别的问题"), owner_actor_id=OWNER)


async def test_second_handover_while_advisor_holds_wheel_is_refused(db):
    _, _, sid = await seed(db)
    async with db() as s:
        led = Ledger(WorkbenchRepository(s))
        await led.handover(handover(sid), owner_actor_id=OWNER)
        with pytest.raises(WheelHeldByOther):
            await led.handover(handover(sid, key="k-0000-0002"), owner_actor_id=OWNER)


async def test_other_user_cannot_touch_session_or_task(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        with pytest.raises(NotFound):
            await led.handover(
                handover(sid), owner_actor_id=OTHER
            )  # AT-05：不泄露存在性
        r = await led.handover(handover(sid), owner_actor_id=OWNER)
        with pytest.raises(NotFound):
            await led.task_view(r["task_id"], owner_actor_id=OTHER)
        with pytest.raises(NotFound):
            await led.request_cancel(r["task_id"], owner_actor_id=OTHER)


async def test_project_root_must_be_inside_environment_roots(db):
    with pytest.raises(RootOutsideWhitelist):
        await seed(db, root="/etc")


async def test_terminal_state_returns_wheel_and_cancels_pending_interactions(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        t = (await led.handover(handover(sid), owner_actor_id=OWNER))["task_id"]
        await led.transition(t, TaskState.VALIDATING)
        await led.transition(t, TaskState.QUEUED)
        it = await led.open_interaction(
            t,
            kind="input",
            prompt=InteractionPrompt(title="澄清", question="哪一年？"),
            waiting_reason=WaitingReason.INPUT,
        )
        await led.transition(t, TaskState.CANCELLED, reason={"by": "test"})
        sess = await repo.get_session(sid)
        assert sess["wheel"] == "user" and sess["active_task_id"] is None
        assert (await repo.get_interaction(it["interaction_id"]))[
            "status"
        ] == "cancelled"
        with pytest.raises(InteractionRejected):
            await led.respond_interaction(
                it["interaction_id"],
                token=it["token"],
                response={"answer": "2025"},
                owner_actor_id=OWNER,
            )
        # 终态不可出（I5）
        with pytest.raises(InvalidTransition):
            await led.transition(t, TaskState.QUEUED)


async def test_interaction_consumption_is_atomic_and_single_use(db):
    _, _, sid = await seed(db)
    async with db() as s:
        led = Ledger(WorkbenchRepository(s))
        t = (await led.handover(handover(sid), owner_actor_id=OWNER))["task_id"]
        await led.transition(t, TaskState.VALIDATING)
        it = await led.open_interaction(
            t,
            kind="input",
            prompt=InteractionPrompt(title="澄清", question="哪一年？"),
            waiting_reason=WaitingReason.INPUT,
        )
        with pytest.raises(InteractionRejected):
            await led.respond_interaction(
                it["interaction_id"],
                token="wrong-token-wrong-token",
                response={},
                owner_actor_id=OWNER,
            )
        with pytest.raises(NotFound):
            await led.respond_interaction(
                it["interaction_id"],
                token=it["token"],
                response={},
                owner_actor_id=OTHER,
            )
        ok = await led.respond_interaction(
            it["interaction_id"],
            token=it["token"],
            response={"answer": "2025"},
            owner_actor_id=OWNER,
        )
        assert ok["state"] == TaskState.QUEUED
        with pytest.raises(InteractionRejected):  # 重复消费
            await led.respond_interaction(
                it["interaction_id"],
                token=it["token"],
                response={"answer": "2025"},
                owner_actor_id=OWNER,
            )


async def test_concurrent_interaction_responses_only_one_wins(db):
    _, _, sid = await seed(db)
    async with db() as s:
        led = Ledger(WorkbenchRepository(s))
        t = (await led.handover(handover(sid), owner_actor_id=OWNER))["task_id"]
        await led.transition(t, TaskState.VALIDATING)
        it = await led.open_interaction(
            t,
            kind="input",
            prompt=InteractionPrompt(title="澄清", question="?"),
            waiting_reason=WaitingReason.INPUT,
        )

    async def respond():
        async with db() as s2:
            try:
                await Ledger(WorkbenchRepository(s2)).respond_interaction(
                    it["interaction_id"],
                    token=it["token"],
                    response={"answer": "x"},
                    owner_actor_id=OWNER,
                )
                return "ok"
            except (InteractionRejected, StaleStateVersion):
                return "rejected"

    results = await asyncio.gather(*(respond() for _ in range(6)))
    assert results.count("ok") == 1


async def test_budget_hard_stop_enters_waiting_resource_and_topup_resumes(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        t = (await led.handover(handover(sid, budget="1.0"), owner_actor_id=OWNER))[
            "task_id"
        ]
        await led.transition(t, TaskState.VALIDATING)
        await led.transition(t, TaskState.QUEUED)
        a = (
            await led.open_attempt(
                t, step_id="rewrite", step_version="1", reserve=Decimal("0.5")
            )
        )["attempt_id"]
        assert (await repo.get_task(t))["state"] == TaskState.RUNNING
        await led.attempt_transition(a, AttemptState.RUNNING, turn_id="turn-1")
        await led.attempt_transition(
            a,
            AttemptState.BUDGET_EXCEEDED,
            consumed=Decimal("1.0"),
            tokens={"input": 9000, "output": 100},
        )
        task = await repo.get_task(t)
        assert (
            task["state"] == TaskState.WAITING
            and task["waiting_reason"] == WaitingReason.RESOURCE
        )
        assert (
            task["active_attempt_id"] is None
            and task["budget"]["used"] == "1.0"
            and task["budget"]["reserved"] == "0"
        )
        pending = await repo.list_interactions(session_id=sid, status="pending")
        assert len(pending) == 1 and pending[0]["kind"] == "resource"
        token = next(
            e["payload"]["token"]
            for e in await repo.list_events(session_id=sid)
            if e["type"] == "interaction/opened"
        )
        r = await led.respond_interaction(
            str(pending[0]["id"]),
            token=token,
            response={"decision": "topup", "amount": "2"},
            owner_actor_id=OWNER,
        )
        assert r["state"] == TaskState.QUEUED
        task = await repo.get_task(t)
        assert task["budget"]["limit"] == "3.0" or task["budget"]["limit"] == "3"
        entries = [e["entry"] for e in await repo.ledger_list(t)]
        assert entries == ["topup", "reserve", "consume", "topup"]


async def test_declining_topup_fails_task_and_returns_wheel(db):
    _, _, sid = await seed(db)
    async with db() as s:
        repo = WorkbenchRepository(s)
        led = Ledger(repo)
        t = (await led.handover(handover(sid, budget="1.0"), owner_actor_id=OWNER))[
            "task_id"
        ]
        await led.transition(t, TaskState.VALIDATING)
        await led.transition(t, TaskState.QUEUED)
        a = (await led.open_attempt(t, step_id="s", step_version="1"))["attempt_id"]
        await led.attempt_transition(a, AttemptState.RUNNING)
        await led.attempt_transition(
            a, AttemptState.BUDGET_EXCEEDED, consumed=Decimal("1.0")
        )
        pending = await repo.list_interactions(session_id=sid, status="pending")
        token = next(
            e["payload"]["token"]
            for e in await repo.list_events(session_id=sid)
            if e["type"] == "interaction/opened"
        )
        r = await led.respond_interaction(
            str(pending[0]["id"]),
            token=token,
            response={"decision": "stop"},
            owner_actor_id=OWNER,
        )
        assert r["state"] == TaskState.FAILED
        assert (await repo.get_session(sid))["wheel"] == "user"


async def test_cancel_vs_complete_only_one_terminal_wins(db):
    _, _, sid = await seed(db)
    async with db() as s:
        led = Ledger(WorkbenchRepository(s))
        t = (await led.handover(handover(sid), owner_actor_id=OWNER))["task_id"]
        await led.transition(t, TaskState.VALIDATING)
        await led.transition(t, TaskState.QUEUED)
        a = (await led.open_attempt(t, step_id="s", step_version="1"))["attempt_id"]
        await led.attempt_transition(a, AttemptState.RUNNING)

    async def complete():
        async with db() as s2:
            led2 = Ledger(WorkbenchRepository(s2))
            try:
                await led2.attempt_transition(a, AttemptState.COMPLETED)
                await led2.transition(t, TaskState.SUCCEEDED)
                return "succeeded"
            except Exception:
                return "lost"

    async def cancel():
        async with db() as s3:
            try:
                led3 = Ledger(WorkbenchRepository(s3))
                r = await led3.request_cancel(t, owner_actor_id=OWNER)
                # 取消直接收成终态，不等编排落实（0034 §三.1）
                return "cancelled" if r["state"] == TaskState.CANCELLED else "noop"
            except Exception:
                return "lost"

    results = await asyncio.gather(complete(), cancel())
    async with db() as s4:
        final = (await WorkbenchRepository(s4).get_task(t))["state"]
    winners = [x for x in results if x in ("succeeded", "cancelled")]
    assert len(winners) == 1, results  # 只有一个终态胜出（AT-13）
    assert final == (
        TaskState.SUCCEEDED if winners[0] == "succeeded" else TaskState.CANCELLED
    )


async def test_restart_can_rebuild_nonterminal_state(db):
    _, _, sid = await seed(db)
    async with db() as s:
        led = Ledger(WorkbenchRepository(s))
        t = (await led.handover(handover(sid), owner_actor_id=OWNER))["task_id"]
        await led.transition(t, TaskState.VALIDATING)
        await led.open_interaction(
            t,
            kind="input",
            prompt=InteractionPrompt(title="a", question="b"),
            waiting_reason=WaitingReason.INPUT,
        )
    async with db() as s2:  # 新进程视角
        repo = WorkbenchRepository(s2)
        open_tasks = await repo.list_nonterminal_tasks()
        assert [str(x["id"]) for x in open_tasks] == [t]
        assert (
            open_tasks[0]["state"] == TaskState.WAITING
            and open_tasks[0]["active_interaction_id"] is not None
        )
        assert (await repo.get_session(sid))["wheel"] == "advisor"
        assert len(await repo.list_interactions(session_id=sid, status="pending")) == 1
