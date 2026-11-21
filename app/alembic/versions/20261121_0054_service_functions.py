"""Immutable provider-owned function contracts in the method skill center."""

from alembic import op

revision = "20261121_0054"
down_revision = "20261121_0053"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE workbench_registry_service_functions (
            provider text NOT NULL,
            name text NOT NULL,
            version integer NOT NULL CHECK (version > 0),
            digest text NOT NULL CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
            contract jsonb NOT NULL,
            PRIMARY KEY (provider, name, version)
        )
    """)


def downgrade():
    op.drop_table("workbench_registry_service_functions")
