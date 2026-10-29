"""委托交付的成稿替用户写进 knowbase 产出区（SDD 0043 第 2.6 节「服务写入路」）。

`workbench_task_outputs`：委托成功时，每份成稿文件（项目里的相对路径）记一行，runner 里一条线
经会合点读用户电脑上的那份、以 knowbench 的服务身份（关系 `knowbase:user-data-output`）写进
knowbase。写不进去不挡交付：记下为什么，隔一阵再试；knowbase 明说不收（类型不对、空间满了）
或过了重试期限就停在 failed，原因留着。写成了记 knowbase 给的资料编号与版本。

run_ref 现在就是委托编号（委托成功是终态，一件委托只交付一次；续跑重进交付也不重记）；
knowbase 按（应用，任务，运行，名字）幂等，重试不会写两份。

downgrade 删表（成稿本身在用户电脑与 knowbase 里，这里只是写没写的账）。

Revision ID: 20261030_0033
Revises: 20261029_0032
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261030_0033"
down_revision = "20261029_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_task_outputs",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "task_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workbench_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_ref", sa.String(64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_try_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.String(200)),
        sa.Column("material_id", sa.String(64)),
        sa.Column("version_no", sa.Integer()),
        sa.Column("sha256", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("written_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'written', 'failed')",
            name="ck_workbench_task_outputs_status",
        ),
        sa.CheckConstraint(
            "(status = 'written') = (material_id IS NOT NULL)",
            name="ck_workbench_task_outputs_written",
        ),
        sa.UniqueConstraint(
            "task_id", "run_ref", "path", name="uq_workbench_task_outputs_file"
        ),
    )
    op.create_index(
        "ix_workbench_task_outputs_due",
        "workbench_task_outputs",
        ["next_try_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_workbench_task_outputs_due", table_name="workbench_task_outputs")
    op.drop_table("workbench_task_outputs")
