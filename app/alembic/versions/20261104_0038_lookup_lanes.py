"""Allow one directory lookup alongside one project execution.

No task data changes. Downgrade requires draining concurrent tasks first;
the old unique index refuses rollback if both lanes are still occupied.
"""

import sqlalchemy as sa

from alembic import op

revision = "20261104_0038"
down_revision = "20261103_0036"
branch_labels = None
depends_on = None

ACTIVE = (
    "project_id IS NOT NULL AND state NOT IN ('SUCCEEDED','REJECTED','FAILED','CANCELLED') "
    "AND NOT (state = 'RECEIVED' AND "
    "coalesce(original_input->'client_context'->>'queued_behind', '') <> '')"
)


def upgrade():
    op.drop_index("uq_workbench_tasks_project_active", table_name="workbench_tasks")
    op.create_index(
        "uq_workbench_tasks_project_active",
        "workbench_tasks",
        ["project_id", sa.text("(profile_id = 'FIND_EXPERT')")],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
    )


def downgrade():
    op.drop_index("uq_workbench_tasks_project_active", table_name="workbench_tasks")
    op.create_index(
        "uq_workbench_tasks_project_active",
        "workbench_tasks",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
    )
