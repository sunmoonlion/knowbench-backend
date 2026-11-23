"""Rename the current session wheel owner while preserving recorded events."""

from alembic import op

revision = "20261123_0061"
down_revision = "20261122_0060"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_workbench_sessions_wheel", "workbench_sessions", type_="check"
    )
    op.execute(
        "UPDATE workbench_sessions SET wheel = 'runtime' WHERE wheel = 'advisor'"
    )
    op.create_check_constraint(
        "ck_workbench_sessions_wheel",
        "workbench_sessions",
        "wheel in ('user', 'runtime')",
    )


def downgrade():
    op.drop_constraint(
        "ck_workbench_sessions_wheel", "workbench_sessions", type_="check"
    )
    op.execute(
        "UPDATE workbench_sessions SET wheel = 'advisor' WHERE wheel = 'runtime'"
    )
    op.create_check_constraint(
        "ck_workbench_sessions_wheel",
        "workbench_sessions",
        "wheel in ('user', 'advisor')",
    )
