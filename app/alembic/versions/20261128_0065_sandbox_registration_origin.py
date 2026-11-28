"""Track sandbox connections explicitly enrolled by a platform operator."""

import sqlalchemy as sa

from alembic import op

revision = "20261128_0065"
down_revision = "20261127_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workbench_sandboxes",
        sa.Column(
            "operator_registered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("workbench_sandboxes", "operator_registered")
