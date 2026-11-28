"""Keep existing run directories and assign new runs a task-specific layout."""

from alembic import op

revision = "20261128_0072"
down_revision = "20261128_0071"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE workbench_tasks ADD COLUMN dossier_version smallint NOT NULL DEFAULT 1"
    )
    op.execute("ALTER TABLE workbench_tasks ALTER COLUMN dossier_version SET DEFAULT 2")


def downgrade():
    op.execute("ALTER TABLE workbench_tasks DROP COLUMN dossier_version")
