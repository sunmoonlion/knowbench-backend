"""Keep verified branch outputs and author receipts with their trial case."""

from alembic import op

revision = "20261121_0055"
down_revision = "20261121_0054"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE workbench_test_cases ADD COLUMN partial_delivery jsonb")


def downgrade():
    op.drop_column("workbench_test_cases", "partial_delivery")
