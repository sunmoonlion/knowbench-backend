"""Select the consumer's local installation for an exact shared tool."""

from alembic import op

revision = "20261120_0050"
down_revision = "20261120_0049"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workbench_incubation_tool_bindings (
        owner_actor_id uuid NOT NULL,
        method_id uuid NOT NULL,
        version integer NOT NULL,
        digest text NOT NULL,
        asset_id uuid NOT NULL,
        PRIMARY KEY(owner_actor_id, method_id, version),
        FOREIGN KEY(method_id,version) REFERENCES workbench_formal_methods(method_id,version)
    )""")


def downgrade():
    op.drop_table("workbench_incubation_tool_bindings")
