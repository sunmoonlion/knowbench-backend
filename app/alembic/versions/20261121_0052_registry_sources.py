"""Immutable, owner-scoped text implementations registered directly in KnowBench."""

from alembic import op

revision = "20261121_0052"
down_revision = "20261121_0051"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workbench_registry_sources (
        owner_actor_id uuid NOT NULL, source_id uuid NOT NULL,
        version integer NOT NULL CHECK (version > 0),
        kind text NOT NULL CHECK (kind IN ('script','instruction')),
        name text NOT NULL, content text NOT NULL, digest text NOT NULL,
        PRIMARY KEY (owner_actor_id, source_id, version)
    )""")


def downgrade():
    op.drop_table("workbench_registry_sources")
