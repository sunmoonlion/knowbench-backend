"""Use collision-resistant names for new author test directories."""

import sqlalchemy as sa
from alembic import op

revision = "20261128_0068"
down_revision = "20261128_0067"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workbench_test_runs",
        sa.Column(
            "folder_scheme", sa.SmallInteger(), nullable=False, server_default="1"
        ),
    )
    op.execute(
        "UPDATE workbench_test_runs SET folder_scheme=2 WHERE started_at IS NULL"
    )
    op.alter_column("workbench_test_runs", "folder_scheme", server_default="2")


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM workbench_test_runs WHERE folder_scheme=2 AND started_at IS NOT NULL AND finished_at IS NULL) THEN
                RAISE EXCEPTION 'finish active author tests before changing directory scheme';
            END IF;
        END $$""")
    op.drop_column("workbench_test_runs", "folder_scheme")
