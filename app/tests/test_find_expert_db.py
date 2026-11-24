"""Retirement of the model-driven historic directory lookup."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from test_knowbot_test_runs_db import ENV, WORKSPACE
from test_workbench_ledger_db import OWNER, ROOT, migrate
from test_workbench_ledger_db import db as db

from app.application.workbench.find_expert import FindExpertRefused, FindExpertService
from app.domain.workbench.expert_lookup import LegacyMatchRetired
from app.infrastructure.workbench.repository import WorkbenchRepository


async def _project(db) -> str:  # type: ignore[no-untyped-def]
    async with db() as s:
        repo = WorkbenchRepository(s)
        async with repo.transaction():
            return await repo.create_project(
                owner_actor_id=OWNER,
                environment_id=ENV,
                workspace_root=WORKSPACE,
                path="finance",
                title="finance",
            )


@pytest.mark.parametrize("version", ["1", "2", "3", "4"])
async def test_retired_match_refused_before_directory_session_or_spend(db, version):
    async with db() as session:
        service = FindExpertService(WorkbenchRepository(session))
        count = text(
            "select (select count(*) from workbench_tasks), (select count(*) from workbench_sessions), (select count(*) from workbench_attempts)"
        )
        before = (await session.execute(count)).one()
        with pytest.raises(LegacyMatchRetired):
            await service.start(
                owner_actor_id=OWNER,
                project_id=str(uuid.uuid4()),
                question="一份汇总表",
                idempotency_key="retired-lookup",
                match_contract=f"knowbench/expert-match/v{version}",
            )
        assert (await session.execute(count)).one() == before


def test_the_migration_goes_down_and_up() -> None:
    """20261029_0032：对话加来源；downgrade 删列，再 upgrade 加回来。"""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set AGENT_TEST_DATABASE_URL to run PostgreSQL workbench tests")
    assert url.rsplit("/", 1)[-1].endswith("_tests")
    path = ROOT / "alembic/versions/20261029_0032_session_origin.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "20261028_0031"
    schema = "wb_test_" + uuid.uuid4().hex
    sync = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))

    def columns() -> set[str]:
        with sync.connect() as connection:
            return {
                c["name"]
                for c in inspect(connection).get_columns(
                    "workbench_sessions", schema=schema
                )
            }

    try:
        with sync.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            migrate(connection)
        assert "origin" in columns()
        for action, present in (("downgrade", False), ("upgrade", True)):
            with sync.begin() as connection:
                connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                with Operations.context(MigrationContext.configure(connection)):
                    getattr(module, action)()
            assert ("origin" in columns()) is present
    finally:
        with sync.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync.dispose()


async def test_named_formal_intake_requires_enabled_owner_before_accessing_registry(db):
    from app.domain.workbench.formal_method_generated import MethodRef

    async with db() as session:
        finder = FindExpertService(
            WorkbenchRepository(session), planning_owners=frozenset({"allowed-user"})
        )
        with pytest.raises(FindExpertRefused):
            await finder.start(
                owner_actor_id=OWNER,
                project_id=str(uuid.uuid4()),
                question="核对差额",
                idempotency_key="off-list-expert",
                match_contract="knowbench/expert-match/v5",
                reference=MethodRef(
                    method_id=uuid.uuid4(), version=1, digest="sha256:" + "a" * 64
                ),
            )
        assert not await session.scalar(text("select count(*) from workbench_tasks"))
