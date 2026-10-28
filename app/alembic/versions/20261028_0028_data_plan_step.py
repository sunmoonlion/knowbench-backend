"""委托第 0 步「定数据」（decisions ⑭）：清单那一行多一种状态 `planning`。

请专家时本人在场先建一张空清单（作者挑选的数据当场签、钉），这一行记成 `planning`；
委托开跑先走第 0 步，模型按专家逻辑和用户问题提出要哪些数据，提完记成 `ready`
（提了的停在步骤之间等用户确认）。

Revision ID: 20261028_0028
Revises: 20261027_0027
"""

from __future__ import annotations

from alembic import op

revision = "20261028_0028"
down_revision = "20261027_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_workbench_task_resolution_status",
        "workbench_task_resolution",
        type_="check",
    )
    op.create_check_constraint(
        "ck_workbench_task_resolution_status",
        "workbench_task_resolution",
        "status IN ('planning', 'ready', 'waiting', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE workbench_task_resolution SET status = 'ready' WHERE status = 'planning'"
    )
    op.drop_constraint(
        "ck_workbench_task_resolution_status",
        "workbench_task_resolution",
        type_="check",
    )
    op.create_check_constraint(
        "ck_workbench_task_resolution_status",
        "workbench_task_resolution",
        "status IN ('ready', 'waiting', 'failed', 'cancelled')",
    )
