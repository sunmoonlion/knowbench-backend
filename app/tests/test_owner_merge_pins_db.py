"""Replay owner consolidation with one non-conflicting immutable pin on PostgreSQL."""

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def test_owner_merge_retains_only_pin():
    import pytest

    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable test database")
    assert url.endswith("_tests")
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    schema = "audit_" + uuid4().hex
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            with Operations.context(MigrationContext.configure(connection)):
                for path in sorted(Path("alembic/versions").glob("20*.py")):
                    spec = spec_from_file_location(path.stem, path)
                    module = module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if module.revision == "20261025_0026":
                        old, new = uuid4(), uuid4()
                        connection.execute(
                            text("""
                            INSERT INTO auth_user(id, issuer, subject, created_at)
                            VALUES (:old, 'first', 'same', now() - interval '1 day'),
                                   (:new, 'second', 'same', now())
                        """),
                            {"old": old, "new": new},
                        )
                        connection.execute(
                            text("""
                            INSERT INTO workbench_knowbot_pins
                            (owner_actor_id,owner_subject,knowbot_id,version,digest,manifest)
                            VALUES (:old,'same',:expert,'1','digest','{}')
                        """),
                            {"old": old, "expert": uuid4()},
                        )
                        module.upgrade()
                        owners = list(
                            connection.scalars(
                                text(
                                    "SELECT owner_actor_id FROM workbench_knowbot_pins"
                                )
                            )
                        )
                        assert owners == [new], f"owners after transfer: {owners}"
                        break
                    module.upgrade()
                else:
                    raise AssertionError("owner migration missing")
            connection.rollback()
    finally:
        engine.dispose()
