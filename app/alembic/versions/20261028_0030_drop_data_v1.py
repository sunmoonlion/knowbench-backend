"""删清单 v1 与旧数据路（decisions ⑰：不留旧版兼容）。

10-28 起工作台只跑 `knowbot-manifest/v2` 的专家；用户的数据只从 knowbase（user-data/v2）
凭委托的运行授权读，钉住的来源只有 `knowbase`：

- 删表：`workbench_session_data`（对话里逐份选的云端知识库，0043 第 4.1 节已退役，
  改成 knowbase 会话）；
- 删行：`workbench_task_data` 里来源是 `cloud`（v1 云端知识库）、`local`（本地知识库）的
  钉住；`workbench_knowbot_pins` 里清单不是 v2 的分身（委托上的快照留着，只是请不了了）；
  `workbench_test_runs` 里清单不是 v2、或带 v1 测试运行授权（`data_grant`）的测试，连同
  用例、答案（外键级联）；
- 收紧约束：`ck_workbench_task_data_source` 只认 `knowbase`；删列 `workbench_task_data.path`
  （只有本地来源用）。

downgrade 建回空表、空列与 0029 时的约束，删掉的行不恢复（数据已不在）。

Revision ID: 20261028_0030
Revises: 20261028_0029
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261028_0030"
down_revision = "20261028_0029"
branch_labels = None
depends_on = None

V2 = "knowbot-manifest/v2"


def upgrade() -> None:
    conn = op.get_bind()
    params = {"v2": V2}
    for statement in (
        "DELETE FROM workbench_task_data WHERE source IN ('cloud', 'local')",
        "DELETE FROM workbench_knowbot_pins"
        " WHERE manifest->>'schema' IS DISTINCT FROM :v2",
        "DELETE FROM workbench_test_runs"
        " WHERE request->'manifest'->>'schema' IS DISTINCT FROM :v2"
        " OR request->'data_grant' IS NOT NULL",
    ):
        conn.execute(sa.text(statement), params)
    op.drop_constraint(
        "ck_workbench_task_data_source", "workbench_task_data", type_="check"
    )
    op.create_check_constraint(
        "ck_workbench_task_data_source",
        "workbench_task_data",
        "source = 'knowbase' AND item_id IS NOT NULL AND grant_id IS NOT NULL"
        " AND version_no IS NOT NULL AND basis IS NOT NULL",
    )
    op.drop_column("workbench_task_data", "path")
    op.drop_table("workbench_session_data")


def downgrade() -> None:
    """建回 0029 时的空表、空列与约束；删掉的 v1 记录不恢复。"""
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
    op.add_column("workbench_task_data", sa.Column("path", sa.String(1024)))
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
