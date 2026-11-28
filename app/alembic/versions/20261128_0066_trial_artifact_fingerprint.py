"""Bind workspace files to the accepted artifact version."""

import sqlalchemy as sa

from alembic import op

revision = "20261128_0066"
down_revision = "20261128_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workbench_artifacts", sa.Column("workspace_sha256", sa.String(64)))


def downgrade() -> None:
    op.drop_column("workbench_artifacts", "workspace_sha256")
