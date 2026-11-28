"""Bind sandbox operations to the installed revision and index receipt identities."""

import sqlalchemy as sa
from alembic import op

revision = "20261128_0070"
down_revision = "20261128_0069"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workbench_sandboxes",
        sa.Column(
            "provision_revision",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.execute("""CREATE INDEX ix_workbench_local_permission_id
        ON workbench_session_events (session_id, (payload->>'id'))
        WHERE event_type='agent/localPermission'""")


def downgrade():
    op.drop_index(
        "ix_workbench_local_permission_id", table_name="workbench_session_events"
    )
    op.drop_column("workbench_sandboxes", "provision_revision")
