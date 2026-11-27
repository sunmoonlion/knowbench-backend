"""Remove storage used exclusively by retired expert workflows."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261127_0064"
down_revision = "20261126_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("workbench_review_decisions")
    op.drop_table("workbench_review_route_confirmations")


def downgrade() -> None:
    op.create_table(
        "workbench_review_route_confirmations",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("case_id", sa.String(64), primary_key=True),
        sa.Column("step", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "case_id"],
            ["workbench_test_cases.run_id", "workbench_test_cases.case_id"],
            ondelete="CASCADE",
        ),
    )
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
