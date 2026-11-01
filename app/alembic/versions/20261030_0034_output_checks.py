"""成稿写进产出区前核对、等数据的领用期限、按 ref 交还授权（独立审 10-30 两轮）。

- `workbench_task_outputs.accepted_sha256`：交付那一刻（`_finish`）经本地 link（`fs/digest`）
  给每份成稿取一次 sha256；写进 knowbase 之前比对，对不上（交付后改过）就不写（0043 第 2.6 节
  「出处照实」）；
- `workbench_task_resolution.checking_until`：等数据的委托由谁在查（到点查的线或 knowfeed
  推送），带条件的 update 抢到期限才往下走，放开时只清自己领的那一次；
- `workbench_task_resolution.grants_released_at` / `release_attempts` / `release_note`：委托
  结束时按这件委托的授权 ref 一次交还（knowbase `release_run`），多签的一起撤掉；交还不成的
  记次数往后排，knowbase 明说不收的记下放弃的原因。

downgrade 删这几列（都是可以重来的账）。

Revision ID: 20261030_0034
Revises: 20261030_0033
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261030_0034"
down_revision = "20261030_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workbench_task_outputs", sa.Column("accepted_sha256", sa.String(64)))
    op.add_column(
        "workbench_task_resolution",
        sa.Column("checking_until", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "workbench_task_resolution",
        sa.Column("grants_released_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "workbench_task_resolution",
        sa.Column("release_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "workbench_task_resolution", sa.Column("release_note", sa.String(200))
    )


def downgrade() -> None:
    op.drop_column("workbench_task_resolution", "release_note")
    op.drop_column("workbench_task_resolution", "release_attempts")
    op.drop_column("workbench_task_resolution", "grants_released_at")
    op.drop_column("workbench_task_resolution", "checking_until")
    op.drop_column("workbench_task_outputs", "accepted_sha256")
