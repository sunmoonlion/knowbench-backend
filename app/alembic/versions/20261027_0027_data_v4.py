"""数据第四版的请专家与会话（SDD 0043 块 6、0048 M2）。

- `workbench_task_resolution`：清单 v2 的专家，一件委托一行。记 knowbase 的解析清单
  （编号、开跑用的修订、使用者 `expert:<分身>@<版本>`、任务参数）、运行授权的 ref、
  用户确认的编号与交给 knowfeed 的需求单号、用户开跑前换过或去掉的；等数据的委托
  由 runner 里一条线按 `next_check_at` 查需求单，齐了重解析、凭确认续签、钉住、接着跑
  （所有者 10-27 定「等数据后自动接着跑」）；`deadline` 是确认的 7 天。
- `workbench_task_data` 加来源 `knowbase`（第四版的资料）：`item_id` 是资料编号，另记
  内容版本、钉的读法与摘要、权利依据；授权仍记在 `grant_id`，委托结束交还。
- `workbench_sessions` 记 knowbase 的会话编号与到期时间：会话里模型首读一份时由
  knowbase 当场签小授权（0048 第五节），不再逐份选。

Revision ID: 20261027_0027
Revises: 20261025_0026
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261027_0027"
down_revision = "20261025_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workbench_task_resolution",
        sa.Column(
            "task_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workbench_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("resolution_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("consumer", sa.String(160), nullable=False),
        sa.Column("params", pg.JSONB(), nullable=False),
        # 运行授权的 ref：模型经工作台读数据时，工作台按它向 knowbase 指明是哪件事
        sa.Column("grant_ref", sa.String(255), nullable=False),
        sa.Column("confirmation_id", sa.String(64)),
        sa.Column("demand_id", sa.String(64)),
        # 用户开跑前换的（条目 → 资料与版本）、去掉的可选条目
        sa.Column("picks", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("dropped", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False),
        # 给页面看的：等的是哪几项、需求单走到哪、没成的原因
        sa.Column("detail", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("checks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'waiting', 'failed', 'cancelled')",
            name="ck_workbench_task_resolution_status",
        ),
    )
    op.create_index(
        "ix_workbench_task_resolution_waiting",
        "workbench_task_resolution",
        ["next_check_at"],
        postgresql_where=sa.text("status = 'waiting'"),
    )

    op.add_column("workbench_task_data", sa.Column("version_no", sa.Integer()))
    op.add_column("workbench_task_data", sa.Column("rendition_kind", sa.String(16)))
    op.add_column("workbench_task_data", sa.Column("rendition_no", sa.Integer()))
    op.add_column("workbench_task_data", sa.Column("rendition_sha256", sa.String(64)))
    op.add_column("workbench_task_data", sa.Column("basis", sa.String(16)))
    op.add_column("workbench_task_data", sa.Column("basis_ref", sa.String(160)))
    op.drop_constraint(
        "ck_workbench_task_data_source", "workbench_task_data", type_="check"
    )
    op.create_check_constraint(
        "ck_workbench_task_data_source",
        "workbench_task_data",
        "(source = 'cloud' AND item_id IS NOT NULL AND grant_id IS NOT NULL"
        " AND path IS NULL)"
        " OR (source = 'local' AND path IS NOT NULL AND item_id IS NULL"
        " AND grant_id IS NULL)"
        " OR (source = 'knowbase' AND item_id IS NOT NULL AND grant_id IS NOT NULL"
        " AND version_no IS NOT NULL AND basis IS NOT NULL AND path IS NULL)",
    )

    op.add_column("workbench_sessions", sa.Column("knowbase_session_id", sa.String(64)))
    op.add_column(
        "workbench_sessions",
        sa.Column("knowbase_session_expires_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("workbench_sessions", "knowbase_session_expires_at")
    op.drop_column("workbench_sessions", "knowbase_session_id")
    op.execute("delete from workbench_task_data where source = 'knowbase'")
    op.drop_constraint(
        "ck_workbench_task_data_source", "workbench_task_data", type_="check"
    )
    op.create_check_constraint(
        "ck_workbench_task_data_source",
        "workbench_task_data",
        "(source = 'cloud' AND item_id IS NOT NULL AND grant_id IS NOT NULL"
        " AND path IS NULL)"
        " OR (source = 'local' AND path IS NOT NULL AND item_id IS NULL"
        " AND grant_id IS NULL)",
    )
    for column in (
        "basis_ref",
        "basis",
        "rendition_sha256",
        "rendition_no",
        "rendition_kind",
        "version_no",
    ):
        op.drop_column("workbench_task_data", column)
    op.drop_table("workbench_task_resolution")
