"""Permit registry source tasks on the existing workbench task ledger."""

from alembic import op

revision = "20261121_0053"
down_revision = "20261121_0052"
branch_labels = None
depends_on = None


def sources(values):
    op.drop_constraint("ck_workbench_tasks_pack_source", "workbench_tasks")
    op.create_check_constraint(
        "ck_workbench_tasks_pack_source",
        "workbench_tasks",
        "(pack_source = 'builtin' and pack_snapshot is null and pack_digest is null)"
        f" or (pack_source in ({values}) and pack_snapshot is not null and pack_digest is not null)",
    )


def upgrade():
    sources("'knowmart','knowlab-test','formal-test','registry-source'")


def downgrade():
    sources("'knowmart','knowlab-test','formal-test'")
