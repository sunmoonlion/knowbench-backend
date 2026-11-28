"""Keep collection retry state independent of the execution deadline."""

import sqlalchemy as sa

from alembic import op

revision = "20261128_0071"
down_revision = "20261128_0070"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workbench_calls", sa.Column("receipt_retry_at", sa.DateTime(timezone=True))
    )
    op.add_column("workbench_calls", sa.Column("receipt_problem", sa.String(128)))


def downgrade():
    op.drop_column("workbench_calls", "receipt_problem")
    op.drop_column("workbench_calls", "receipt_retry_at")
