"""Account-scoped reviewer choice, independent of ordinary thread settings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261122_0057"
down_revision = "20261122_0056"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workbench_user_prefs", sa.Column("reviewer", postgresql.JSONB()))

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


def downgrade():
    op.drop_table("workbench_review_route_confirmations")
    op.drop_column("workbench_user_prefs", "reviewer")
