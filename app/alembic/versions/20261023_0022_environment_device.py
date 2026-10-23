"""本地 link 的设备编号、主机名、系统（账 143，会合点备忘第三节）。

0.5.2 起本地 link 报设备编号：「这台电脑上的这一份本地 link」按编号认，不再按电脑名（同名的两台不会并成一条）。
`first_host` 是第一次登记这个编号时的主机名：配置目录被整个拷到别的电脑，编号会跟过去，主机名对不上就提示。
老代理不报编号，照旧按名字登记；新代理第一次报到时认领同名、还没编号的那条（项目挂在它上面）。

Revision ID: 20261023_0022
Revises: 20261019_0021
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261023_0022"
down_revision = "20261019_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workbench_environments", sa.Column("device_id", sa.String(36)))
    op.add_column("workbench_environments", sa.Column("host", sa.String(128)))
    op.add_column("workbench_environments", sa.Column("os", sa.String(64)))
    op.add_column("workbench_environments", sa.Column("first_host", sa.String(128)))
    op.create_index(
        "uq_workbench_environments_owner_device",
        "workbench_environments",
        ["owner_actor_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("device_id is not null"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_workbench_environments_owner_device", table_name="workbench_environments"
    )
    for column in ("first_host", "os", "host", "device_id"):
        op.drop_column("workbench_environments", column)
