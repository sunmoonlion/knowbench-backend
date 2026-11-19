"""Short-lived, single-use authorizations for external user connections."""

from alembic import op

revision = "20261120_0047"
down_revision = "20261119_0046"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workbench_connection_oauth (
        state_hash text PRIMARY KEY, owner_actor_id uuid NOT NULL, provider_id text NOT NULL,
        verifier text NOT NULL, context jsonb NOT NULL, expires_at timestamptz NOT NULL
    )""")
    op.execute("CREATE INDEX ix_connection_oauth_expiry ON workbench_connection_oauth(expires_at)")


def downgrade():
    op.drop_table("workbench_connection_oauth")
