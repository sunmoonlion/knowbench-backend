"""Tests remain executable through knowlab while ordinary workspace reads exclude them."""

import pytest
from sqlalchemy import text
from test_knowbot_test_runs_db import (
    AUTHOR,
    ENV,
    WORKSPACE,
    accept,
    request,
    setup_test_computer,
)
from test_workbench_advisor_db import ScriptedAppServer
from test_workbench_ledger_db import OTHER, OWNER
from test_workbench_ledger_db import db as db

from app.application.workbench.session_service import SessionService
from app.application.workbench.test_execution import execution_view
from app.application.workbench.test_runs import KnowbotTestConductor
from app.bootstrap.workbench import workbench_stores
from app.domain.workbench.errors import NotFound
from app.domain.workbench.errors import TestManagedInKnowlab as ManagedInKnowlab
from app.infrastructure.workbench.repository import WorkbenchRepository


@pytest.mark.parametrize("legacy", [False, True])
async def test_test_history_hidden_from_browser_but_available_to_author_runtime(
    db, legacy
):
    fake = ScriptedAppServer([])
    await fake.start()
    run = "knowlab-surface-0001"
    try:
        await setup_test_computer(db, fake)
        await accept(db, request(run))
        await KnowbotTestConductor(workbench_stores(db)).tick()
        async with db() as session:
            internal = WorkbenchRepository(session)
            browser = WorkbenchRepository(session, browser_view=True)
            [task] = await internal.list_tasks(owner_actor_id=OWNER)
            tid, sid, pid = map(
                str, (task["id"], task["session_id"], task["project_id"])
            )
            if legacy:
                # Formal case binding identifies old tests even without source tags.
                await session.execute(
                    text("update workbench_sessions set origin = null where id = :id"),
                    {"id": sid},
                )
                await session.execute(
                    text(
                        "update workbench_tasks set pack_source = 'knowmart' where id = :id"
                    ),
                    {"id": tid},
                )
                await session.commit()
            assert await browser.list_projects(owner_actor_id=OWNER) == []
            assert await browser.list_sessions(owner_actor_id=OWNER) == []
            assert await browser.list_tasks(owner_actor_id=OWNER) == []
            for read, key in [
                (browser.get_task, tid),
                (browser.get_session, sid),
                (browser.get_project, pid),
            ]:
                with pytest.raises(ManagedInKnowlab):
                    await read(key, owner_actor_id=OWNER)
                with pytest.raises(NotFound):
                    await read(key, owner_actor_id=OTHER)
            assert (await internal.get_task(tid))["id"] == task["id"]
            if legacy:
                # A mismatched binding is hidden, but must not become executable.
                with pytest.raises(NotFound):
                    await execution_view(internal, run, owner_subject=AUTHOR)
                await session.execute(
                    text(
                        "update workbench_tasks set pack_source = 'knowlab-test' where id = :id"
                    ),
                    {"id": tid},
                )
                await session.commit()
            view = await execution_view(internal, run, owner_subject=AUTHOR)
            assert view["cases"][0]["case_id"] == "c1"
            async with internal.transaction():
                ordinary = await internal.create_project(
                    owner_actor_id=OWNER,
                    environment_id=ENV,
                    workspace_root=WORKSPACE,
                    path="ordinary",
                    title="ordinary",
                )
            normal = await SessionService(internal).start(
                owner_actor_id=OWNER,
                kind="chat",
                project_id=ordinary,
            )
            [project] = await browser.list_projects(owner_actor_id=OWNER)
            assert str(project["id"]) == ordinary
            [conversation] = await browser.list_sessions(owner_actor_id=OWNER)
            assert str(conversation["id"]) == normal["session_id"]
    finally:
        await fake.close()


async def test_migration_replay_keeps_shared_project_and_test_history(db):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).parents[1]
        / "alembic/versions/20261106_0039_test_project_surface.py"
    )
    spec = importlib.util.spec_from_file_location("surface_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()

    fake = ScriptedAppServer([])
    await fake.start()
    try:
        await setup_test_computer(db, fake)
        await accept(db, request("knowlab-surface-replay"))
        await KnowbotTestConductor(workbench_stores(db)).tick()
        async with db() as session:
            repo = WorkbenchRepository(session)
            [task] = await repo.list_tasks(owner_actor_id=OWNER)
            pid = str(task["project_id"])
            normal = await SessionService(repo).start(
                owner_actor_id=OWNER,
                kind="chat",
                project_id=pid,
            )
            connection = await session.connection()
            await connection.run_sync(migrate, "downgrade")
            await connection.run_sync(migrate, "upgrade")
            await session.commit()
            browser = WorkbenchRepository(session, browser_view=True)
            [project] = await browser.list_projects(owner_actor_id=OWNER)
            assert str(project["id"]) == pid and project["conversations"] == 1
            [conversation] = await browser.list_sessions(owner_actor_id=OWNER)
            assert str(conversation["id"]) == normal["session_id"]
            assert await browser.list_tasks(owner_actor_id=OWNER) == []
            assert len(await repo.list_tasks(owner_actor_id=OWNER)) == 1
    finally:
        await fake.close()
