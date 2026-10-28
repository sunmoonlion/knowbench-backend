"""作为运行方跑孵化的一轮（专家孵化方案第八节，契约 knowbot-incubation/v1）。

一轮一行：knowlab 交来的请求原文与摘要（同一个 incubation_id 只认一份内容）、状态、在等什么、
作者「孵化」项目里的那段对话与委托、花费、合契约的交回物。委托本身是作者工作台里普通的
内置专家委托（孵化方法 INCUBATE，pack_source = builtin），不改委托表。

downgrade 删表（孵化的记录在 knowlab 那边，这里只是运行方的账）。

Revision ID: 20261028_0031
Revises: 20261028_0030
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261028_0031"
down_revision = "20261028_0030"
branch_labels = None
depends_on = None


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "workbench_incubations",
        sa.Column("incubation_id", sa.String(128), primary_key=True),
        sa.Column("caller", sa.String(32), nullable=False),
        sa.Column("owner_subject", sa.String(512), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("request", pg.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        # 还没开跑或跑着时在等作者的电脑：computer_offline、folder_not_ready
        sa.Column("waiting_for", sa.String(32)),
        sa.Column("budget_limit", sa.Numeric(12, 2), nullable=False),
        sa.Column("spent", sa.Numeric(14, 6), nullable=False, server_default="0"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("project_id", pg.UUID(as_uuid=True)),
        sa.Column("session_id", pg.UUID(as_uuid=True)),
        sa.Column("task_id", pg.UUID(as_uuid=True)),
        sa.Column("handback", pg.JSONB()),
        sa.Column("error", sa.Text()),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'over_budget',"
            " 'cancelled')",
            name="ck_workbench_incubations_status",
        ),
        sa.CheckConstraint(
            "waiting_for IS NULL OR waiting_for IN ('computer_offline',"
            " 'folder_not_ready')",
            name="ck_workbench_incubations_waiting_for",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (handback IS NOT NULL)",
            name="ck_workbench_incubations_handback",
        ),
    )
    op.create_index(
        "ix_workbench_incubations_status", "workbench_incubations", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_workbench_incubations_status", table_name="workbench_incubations")
    op.drop_table("workbench_incubations")
