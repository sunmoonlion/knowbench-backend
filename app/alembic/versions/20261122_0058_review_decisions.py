"""Keep the owner's decisions separately from the reviewer's sealed output."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261122_0058"
down_revision = "20261122_0057"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workbench_review_decisions",
        sa.Column(
            "task_id",
            sa.Uuid(),
            sa.ForeignKey("workbench_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("receipt", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("workbench_review_decisions")
