"""分身测试每条用例的产出：成稿与各步交回物（SDD 0037）。

用例跑完、记结果时取好存这里（用例共用工作区，晚了成稿会被下一条盖掉），knowlab 经
`/test-runs/{run_id}/cases/{case_id}/outputs` 取走；整次测试收尾 30 天后清空，结果留着。

Revision ID: 20261019_0021
Revises: 20261017_0020
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20261019_0021"
down_revision = "20261017_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workbench_test_cases", sa.Column("outputs", pg.JSONB()))


def downgrade() -> None:
    op.drop_column("workbench_test_cases", "outputs")
