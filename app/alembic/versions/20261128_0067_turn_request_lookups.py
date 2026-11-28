"""Index user turn replays and pre-indexed command receipts by session and request."""

from alembic import op

revision = "20261128_0067"
down_revision = "20261128_0066"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE INDEX ix_workbench_turn_request ON workbench_session_events
        (session_id,(payload->>'request_id'),cursor) WHERE event_type='turn/requested'""")
    op.execute("""CREATE INDEX ix_workbench_turn_command ON workbench_commands
        (session_id,(payload->>'request_id')) WHERE kind='turn.start'""")


def downgrade():
    op.drop_index("ix_workbench_turn_command", table_name="workbench_commands")
    op.drop_index("ix_workbench_turn_request", table_name="workbench_session_events")
