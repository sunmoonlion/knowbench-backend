"""Index the outstanding execution units retained for receipt reconciliation."""

from alembic import op

revision = "20261121_0051"
down_revision = "20261120_0050"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE INDEX workbench_instruction_cleanup_idx ON workbench_attempts
        (task_id) WHERE ended_at IS NOT NULL AND refs->'instruction_unit'->>'pending'='true'""")


def downgrade():
    op.drop_index("workbench_instruction_cleanup_idx", table_name="workbench_attempts")
