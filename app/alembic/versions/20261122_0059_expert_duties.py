"""Periodically open one ordinary expert request on an owner's accepted delivery."""

from alembic import op

revision = "20261122_0059"
down_revision = "20261122_0058"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE workbench_expert_duties (
            origin uuid PRIMARY KEY REFERENCES workbench_tasks ON DELETE CASCADE,
            current_match uuid NOT NULL REFERENCES workbench_tasks ON DELETE CASCADE,
            config jsonb NOT NULL, approved jsonb, paused boolean NOT NULL DEFAULT false,
            next_at timestamptz NOT NULL, retry_at timestamptz, problem text
        )
    """)
    op.execute("""CREATE INDEX workbench_expert_duties_due ON workbench_expert_duties
        (next_at) WHERE NOT paused AND approved IS NOT NULL""")
    op.execute("""CREATE INDEX workbench_formal_handoff_versions ON workbench_formal_methods
        ((document#>>'{validation,request_id}'), method_id, version)
        WHERE caller='knowlab' AND document#>>'{draft,definition,procedure,kind}'='orchestration'""")


def downgrade():
    op.drop_index("workbench_formal_handoff_versions")
    op.drop_table("workbench_expert_duties")
