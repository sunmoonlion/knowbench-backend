"""测试委托的第 0 步（SDD 0046 第三节）：清单那一行可以没有 knowbase 的清单。

清单 v2 的草稿测试时，用例没带测试集的委托先跑第 0 步；提出的数据不经运行方的账号去
knowbase 解析，而报给 knowlab 由作者确认，所以这一行的清单编号与修订是空的。

Revision ID: 20261028_0029
Revises: 20261028_0028
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261028_0029"
down_revision = "20261028_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "workbench_task_resolution",
        "resolution_id",
        existing_type=sa.String(64),
        nullable=True,
    )
    op.alter_column(
        "workbench_task_resolution",
        "revision",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("DELETE FROM workbench_task_resolution WHERE resolution_id IS NULL")
    op.alter_column(
        "workbench_task_resolution",
        "revision",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "workbench_task_resolution",
        "resolution_id",
        existing_type=sa.String(64),
        nullable=False,
    )
