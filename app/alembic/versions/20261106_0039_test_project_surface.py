"""Separate test projects from the ordinary browser workspace without deleting history."""

import sqlalchemy as sa

from alembic import op

revision = "20261106_0039"
down_revision = "20261104_0038"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "ck_workbench_sessions_origin", "workbench_sessions", type_="check"
    )
    op.create_check_constraint(
        "ck_workbench_sessions_origin",
        "workbench_sessions",
        "origin IS NULL OR origin IN ('direct_answer', 'knowlab-test')",
    )
    op.add_column(
        "workbench_projects",
        sa.Column("purpose", sa.String(32), nullable=False, server_default="user"),
    )
    op.create_check_constraint(
        "ck_workbench_project_purpose",
        "workbench_projects",
        "purpose in ('user', 'knowlab-test')",
    )
    # Use stored source and formal case bindings, never titles/account names/paths.
    op.execute("""update workbench_sessions s set origin = 'knowlab-test'
        where exists (select 1 from workbench_tasks t where t.session_id = s.id
          and (t.pack_source = 'knowlab-test' or exists (select 1 from workbench_test_cases c where c.task_id = t.id)))
        and not exists (select 1 from workbench_tasks t where t.session_id = s.id
          and coalesce(t.pack_source, '') <> 'knowlab-test' and not exists (select 1 from workbench_test_cases c where c.task_id = t.id))""")
    # Shared projects stay visible; only exclusively test sessions qualify.
    op.execute("""update workbench_projects p set purpose = 'knowlab-test'
        where exists (select 1 from workbench_sessions s where s.project_id = p.id and s.origin = 'knowlab-test')
        and not exists (select 1 from workbench_sessions s where s.project_id = p.id and coalesce(s.origin, '') <> 'knowlab-test')""")


def downgrade():
    op.drop_constraint(
        "ck_workbench_project_purpose", "workbench_projects", type_="check"
    )
    op.drop_column("workbench_projects", "purpose")
    # Source pack and test-case bindings remain authoritative; restore the old enum.
    op.execute(
        "update workbench_sessions set origin = null where origin = 'knowlab-test'"
    )
    op.drop_constraint(
        "ck_workbench_sessions_origin", "workbench_sessions", type_="check"
    )
    op.create_check_constraint(
        "ck_workbench_sessions_origin",
        "workbench_sessions",
        "origin IS NULL OR origin IN ('direct_answer')",
    )
