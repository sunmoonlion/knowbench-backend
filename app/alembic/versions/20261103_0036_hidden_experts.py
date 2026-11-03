"""用户移除工作台专家列表项；不可变专家快照与任务保留。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261103_0036"
down_revision = "20261102_0035"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workbench_hidden_experts",
        sa.Column("owner_actor_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", sa.String(128), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    # 仅移除列表偏好；回退后被移除的专家会重新显示，不丢任务或专家快照。
    op.drop_table("workbench_hidden_experts")
