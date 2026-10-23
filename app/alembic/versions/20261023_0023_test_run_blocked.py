"""分身测试没开跑时在等什么（账 141）。

测试电脑不在线、或给不出干净的目录时，测试停在「已受理」。原来 knowbench 一行日志都不打，knowlab 管理端也只写
「已受理」。`blocked_reason` 记原因（computer_offline / folder_not_ready），随测试结果给 knowlab；开跑时清掉。
`blocked_logged_at` 是上次为它记日志的时间：同一原因每 10 分钟记一行，原因变了马上记。

Revision ID: 20261023_0023
Revises: 20261023_0022
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261023_0023"
down_revision = "20261023_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workbench_test_runs", sa.Column("blocked_reason", sa.String(32)))
    op.add_column(
        "workbench_test_runs",
        sa.Column("blocked_logged_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("workbench_test_runs", "blocked_logged_at")
    op.drop_column("workbench_test_runs", "blocked_reason")
