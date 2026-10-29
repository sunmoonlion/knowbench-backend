"""对话记来源（请专家方案设计第 4.9 节第 5 条；decisions 10-29「AI 请专家」）。

`workbench_sessions.origin`：这段对话是怎么来的。现在只有一种：`direct_answer`——找专家没找到
合适的，用户点了「让我的模型直接答」开的对话；页面在回答上方照实标「这是你的模型直接答的，
不是专家，方法没经过测试」。别的对话为空。

downgrade 删这一列。

Revision ID: 20261029_0032
Revises: 20261028_0031
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261029_0032"
down_revision = "20261028_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workbench_sessions", sa.Column("origin", sa.String(32)))
    op.create_check_constraint(
        "ck_workbench_sessions_origin",
        "workbench_sessions",
        "origin IS NULL OR origin IN ('direct_answer')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_workbench_sessions_origin", "workbench_sessions", type_="check"
    )
    op.drop_column("workbench_sessions", "origin")
