"""Accepted method plan pointer; plan history remains in task artifacts.

No fixed task needs backfill. Downgrade refuses to discard accepted plan pointers.
Revision ID: 20261102_0035
Revises: 20261030_0034
"""

import sqlalchemy as sa

from alembic import op

revision = "20261102_0035"
down_revision = "20261030_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workbench_tasks", sa.Column("accepted_plan_version", sa.Integer()))
    op.create_check_constraint(
        "ck_workbench_tasks_plan_version",
        "workbench_tasks",
        "accepted_plan_version IS NULL OR accepted_plan_version > 0",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM workbench_tasks WHERE accepted_plan_version IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "cannot discard accepted method plans; preserve task history"
        )
    op.drop_constraint(
        "ck_workbench_tasks_plan_version", "workbench_tasks", type_="check"
    )
    op.drop_column("workbench_tasks", "accepted_plan_version")
