"""User connections, immutable skill versions, selections and external call receipts."""
from alembic import op

revision = "20261113_0040"
down_revision = "20261106_0039"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""CREATE TABLE workbench_capability_assets (
      id uuid PRIMARY KEY, owner_actor_id uuid NOT NULL, kind text NOT NULL CHECK (kind IN ('connection','skill')),
      name text NOT NULL, revision integer NOT NULL DEFAULT 1, status text NOT NULL,
      default_selected boolean NOT NULL DEFAULT false, detail jsonb NOT NULL,
      secret text, request_id text NOT NULL, request_digest text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(owner_actor_id, request_id))""")
    op.execute("""CREATE TABLE workbench_capability_selections (
      session_id uuid PRIMARY KEY REFERENCES workbench_sessions(id) ON DELETE CASCADE, owner_actor_id uuid NOT NULL,
      revision integer NOT NULL DEFAULT 0, detail jsonb NOT NULL DEFAULT '{}',
      runtime jsonb NOT NULL DEFAULT '{}', updated_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE workbench_capability_calls (
      id uuid PRIMARY KEY, owner_actor_id uuid NOT NULL, session_id uuid NOT NULL REFERENCES workbench_sessions(id) ON DELETE CASCADE,
      connection_id uuid NOT NULL REFERENCES workbench_capability_assets(id),
      interaction_id uuid REFERENCES workbench_interactions(id) ON DELETE SET NULL,
      digest text NOT NULL, status text NOT NULL, detail jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(session_id, digest))""")
    op.execute("CREATE INDEX ix_capability_assets_owner ON workbench_capability_assets(owner_actor_id, created_at)")
    op.execute("CREATE INDEX ix_capability_calls_session ON workbench_capability_calls(session_id, created_at)")

def downgrade():
    op.drop_table("workbench_capability_calls")
    op.drop_table("workbench_capability_selections")
    op.drop_table("workbench_capability_assets")
