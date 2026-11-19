"""Keep explicit, revocable read approval on its original action receipt."""

from alembic import op

revision = "20261120_0048"
down_revision = "20261120_0047"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE workbench_calls ADD COLUMN read_scope_key text,
        ADD COLUMN read_scope_until timestamptz, ADD COLUMN read_revoked_at timestamptz,
        ADD CONSTRAINT ck_call_read_scope CHECK ((read_scope_key IS NULL) = (read_scope_until IS NULL)
          AND (read_revoked_at IS NULL OR read_scope_key IS NOT NULL))""")
    op.execute("""CREATE INDEX ix_call_read_scope ON workbench_calls(owner_actor_id,session_id,read_scope_key)
        WHERE read_scope_key IS NOT NULL AND read_revoked_at IS NULL""")


def downgrade():
    op.execute(
        "ALTER TABLE workbench_calls DROP COLUMN read_scope_key, DROP COLUMN read_scope_until, DROP COLUMN read_revoked_at"
    )
