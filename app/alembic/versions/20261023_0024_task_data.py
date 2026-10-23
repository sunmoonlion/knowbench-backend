"""请专家用自己的数据（SDD 0042 第五节）：这件委托钉住的那几份。

请专家那一刻，用户按专家的数据需求声明从本地知识库（A，本地 link 算 sha256）或云端知识库
（B，knowbase 的资料编号与摘要、这件委托的运行授权）选的每一份记一行。专家只能读这几份，
读时再核摘要；数值验收按钉住的内容复核。委托结束后交还运行授权（`grant_released_at`）。`dataset_id` 是工作台给这一份起的编号，读数据工具、
格子地址、引文都用它（形状与原来 knowlab 的数据编号一样）。

Revision ID: 20261023_0024
Revises: 20261023_0023
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261023_0024"
down_revision = "20261023_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_task_data",
        sa.Column("dataset_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workbench_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        # 声明里的哪一样（如「年报」）；旧格式的专家没有声明
        sa.Column("slot", sa.String(80)),
        sa.Column("source", sa.String(8), nullable=False),
        # B：knowbase 的资料编号与这件委托的运行授权
        sa.Column("item_id", sa.String(64)),
        sa.Column("grant_id", sa.String(64)),
        # 委托结束后向 knowbase 交还了运行授权的时间（没交还的由 runner 补）
        sa.Column("grant_released_at", sa.DateTime(timezone=True)),
        # A：用户电脑上的绝对路径（本地知识库目录里）
        sa.Column("path", sa.String(1024)),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(source = 'cloud' AND item_id IS NOT NULL AND grant_id IS NOT NULL"
            " AND path IS NULL)"
            " OR (source = 'local' AND path IS NOT NULL AND item_id IS NULL"
            " AND grant_id IS NULL)",
            name="ck_workbench_task_data_source",
        ),
        sa.UniqueConstraint(
            "task_id", "position", name="uq_workbench_task_data_position"
        ),
    )
    op.create_index(
        "ix_workbench_task_data_unreleased",
        "workbench_task_data",
        ["grant_id"],
        postgresql_where=sa.text("grant_id IS NOT NULL AND grant_released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("workbench_task_data")
