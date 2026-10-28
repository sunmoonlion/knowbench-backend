"""迁移 20261028_0030（decisions ⑰）：v1 的钉住（云端、本地）、清单 v1 的分身与测试删掉，
对话选的云端知识库那张表删掉，钉住的来源只认 knowbase；第四版的都留着。真 PostgreSQL、
一次性 schema：先迁到 0029 造数，再单跑这一个迁移的 upgrade 与 downgrade。"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from test_workbench_ledger_db import OWNER, ROOT, handover, seed

from app.application.workbench.ledger import Ledger
from app.infrastructure.storage.postgres import make_session_factory
from app.infrastructure.workbench.repository import WorkbenchRepository

TARGET = ROOT / "alembic/versions/20261028_0030_drop_data_v1.py"
BEFORE = [p for p in sorted((ROOT / "alembic/versions").glob("20*.py")) if p != TARGET]
KNOWBOT_OLD, KNOWBOT_NEW = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))


def _run(connection, paths, action: str = "upgrade") -> None:  # type: ignore[no-untyped-def]
    with Operations.context(MigrationContext.configure(connection)):
        for path in paths:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            getattr(module, action)()


@pytest_asyncio.fixture
async def at_0029():  # type: ignore[no-untyped-def]
    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set AGENT_TEST_DATABASE_URL to run PostgreSQL workbench tests")
    assert url.rsplit("/", 1)[-1].endswith("_tests")
    schema = "wb_test_" + uuid.uuid4().hex
    sync = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    with sync.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _run(connection, BEFORE)
    engine = create_async_engine(
        url.replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"search_path": schema}},
    )

    def migrate(action: str) -> None:
        with sync.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            _run(connection, [TARGET], action)

    try:
        yield make_session_factory(engine), migrate
    finally:
        await engine.dispose()
        with sync.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        sync.dispose()


PIN = {"media_type": "application/pdf", "size_bytes": 1, "sha256": "a" * 64}


async def _rows(factory) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """一件委托钉了三份（v1 云端、v1 本地、第四版 knowbase）；对话选了一份云端的；
    清单 v1 与 v2 的分身各钉一版；测试三次：v1、v2、v2 却带 v1 的测试运行授权。"""
    _, _, sid = await seed(factory)
    async with factory() as s:
        task = (
            await Ledger(WorkbenchRepository(s)).handover(
                handover(sid), owner_actor_id=OWNER
            )
        )["task_id"]
    async with factory() as s, s.begin():
        for position, (source, extra) in enumerate(
            (
                ("cloud", {"item": "i-cloud", "grant": "g-1", "path": None}),
                ("local", {"item": None, "grant": None, "path": "C:\\年报.pdf"}),
                ("knowbase", {"item": "m-1", "grant": "g-2", "path": None}),
            )
        ):
            await s.execute(
                text(
                    "INSERT INTO workbench_task_data (task_id, dataset_id, position,"
                    " source, item_id, grant_id, path, name, media_type, size_bytes,"
                    " sha256, version_no, basis) VALUES (:t, :d, :p, :src, :item,"
                    " :grant, :path, :n, :m, :b, :h, :vn, :basis)"
                ),
                {
                    "t": task,
                    "d": uuid.uuid4(),
                    "p": position,
                    "src": source,
                    "n": f"{source}.pdf",
                    "m": PIN["media_type"],
                    "b": PIN["size_bytes"],
                    "h": PIN["sha256"],
                    "vn": 1 if source == "knowbase" else None,
                    "basis": "frozen_binding" if source == "knowbase" else None,
                    **extra,
                },
            )
        await s.execute(
            text(
                "INSERT INTO workbench_session_data (session_id, position, item_id,"
                " grant_id, name, media_type, size_bytes, sha256, expires_at) VALUES"
                " (:s, 0, 'i-cloud', 'g-3', '年报.pdf', :m, 1, :h, now())"
            ),
            {"s": sid, "m": PIN["media_type"], "h": PIN["sha256"]},
        )
        for knowbot, schema in (
            (KNOWBOT_OLD, "knowbot-manifest/v1"),
            (KNOWBOT_NEW, "knowbot-manifest/v2"),
        ):
            await s.execute(
                text(
                    "INSERT INTO workbench_knowbot_pins (owner_actor_id, owner_subject,"
                    " knowbot_id, version, digest, manifest) VALUES (:o, 'user-1', :k,"
                    " '1', 'sha256:x', CAST(:m AS jsonb))"
                ),
                {"o": OWNER, "k": knowbot, "m": json.dumps({"schema": schema})},
            )
        for run_id, request in (
            ("run-v1-0001", {"manifest": {"schema": "knowbot-manifest/v1"}}),
            ("run-v2-0001", {"manifest": {"schema": "knowbot-manifest/v2"}}),
            (
                "run-v2-grant",
                {
                    "manifest": {"schema": "knowbot-manifest/v2"},
                    "data_grant": {"grant_id": "g-4", "items": []},
                },
            ),
        ):
            await s.execute(
                text(
                    "INSERT INTO workbench_test_runs (run_id, caller, owner_subject,"
                    " digest, request, budget_limit, timeout_seconds, max_wait_seconds)"
                    " VALUES (:r, 'knowlab', 'user-1', 'd', CAST(:q AS jsonb), 10,"
                    " 600, 600)"
                ),
                {"r": run_id, "q": json.dumps(request)},
            )
            await s.execute(
                text(
                    "INSERT INTO workbench_test_cases (run_id, case_id, position)"
                    " VALUES (:r, 'c1', 0)"
                ),
                {"r": run_id},
            )
            await s.execute(
                text(
                    "INSERT INTO workbench_test_answers (run_id, question_id, answer,"
                    " answered_by) VALUES (:r, 'q1', '{}', 'user')"
                ),
                {"r": run_id},
            )
    return task, sid


async def _all(factory, sql: str) -> list:  # type: ignore[no-untyped-def]
    async with factory() as s:
        return [tuple(r) for r in (await s.execute(text(sql))).all()]


async def _exists(factory, table: str) -> bool:  # type: ignore[no-untyped-def]
    found = await _all(factory, f"SELECT to_regclass('{table}')")
    return found[0][0] is not None


async def _columns(factory, table: str) -> set[str]:  # type: ignore[no-untyped-def]
    return {
        r[0]
        for r in await _all(
            factory,
            "SELECT column_name FROM information_schema.columns WHERE table_schema"
            f" = current_schema() AND table_name = '{table}'",
        )
    }


async def test_v1_records_go_and_the_fourth_edition_stays(at_0029) -> None:
    factory, migrate = at_0029
    task, _ = await _rows(factory)
    migrate("upgrade")
    # 钉住的只剩 knowbase 那份；path 这一列没了
    assert await _all(factory, "SELECT source, item_id FROM workbench_task_data") == [
        ("knowbase", "m-1")
    ]
    assert "path" not in await _columns(factory, "workbench_task_data")
    assert not await _exists(factory, "workbench_session_data")
    assert await _all(
        factory, "SELECT knowbot_id::text FROM workbench_knowbot_pins"
    ) == [(KNOWBOT_NEW,)]
    # 测试只剩 v2 的那次；v1 的、带 v1 测试运行授权的连同用例与答案都删了
    for table in (
        "workbench_test_runs",
        "workbench_test_cases",
        "workbench_test_answers",
    ):
        assert await _all(factory, f"SELECT run_id FROM {table}") == [("run-v2-0001",)]
    # 委托本身留着
    assert await _all(factory, "SELECT id::text FROM workbench_tasks") == [(task,)]
    # 约束只认 knowbase：再钉 v1 的来源进不去
    async with factory() as s:
        repo = WorkbenchRepository(s)
        with pytest.raises(IntegrityError):
            async with repo.transaction():
                await repo.pin_task_data(
                    task,
                    [
                        {
                            "source": "cloud",
                            "item_id": "i",
                            "grant_id": "g",
                            **PIN,
                            "name": "x.pdf",
                        }
                    ],
                )

    # 退回：空表、空列与旧约束建回来，删掉的行不恢复
    migrate("downgrade")
    assert await _exists(factory, "workbench_session_data")
    assert await _all(factory, "SELECT count(*) FROM workbench_session_data") == [(0,)]
    assert "path" in await _columns(factory, "workbench_task_data")
    assert await _all(factory, "SELECT source FROM workbench_task_data") == [
        ("knowbase",)
    ]
    assert await _all(factory, "SELECT run_id FROM workbench_test_runs") == [
        ("run-v2-0001",)
    ]
