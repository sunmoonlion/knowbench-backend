"""Pin each install ticket to the issued release and installer template."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261128_0069"
down_revision = "20261128_0068"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workbench_agent_install_credentials", sa.Column("release_snapshot", JSONB()))


def downgrade():
    # Older readers cannot enforce release identity for still-valid tickets.
    op.execute("UPDATE workbench_agent_install_credentials SET expires_at=now() WHERE expires_at>now()")
    op.drop_column("workbench_agent_install_credentials", "release_snapshot")
