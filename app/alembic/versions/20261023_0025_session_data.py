"""聊天、工作里选用云端知识库的几份（SDD 0042 第四节）。

用户在一段对话里选云端知识库（B）的几份给模型读：每段对话一组，凭一条「会话」运行授权
（knowbase 签，只读这几份、钉这几个摘要）。改选就换一条授权、交还旧的；兜底按授权的时限
（`expires_at`），过了要用户再选。模型用 `list_library`、`read_library_item` 读。

Revision ID: 20261023_0025
Revises: 20261023_0024
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261023_0025"
down_revision = "20261023_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_session_data",
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workbench_sessions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "session_id", "item_id", name="uq_workbench_session_data_item"
        ),
    )


def downgrade() -> None:
    op.drop_table("workbench_session_data")
