"""One call ledger; installation identity is distinct from a connection account."""

from alembic import op

revision = "20261114_0041"
down_revision = "20261113_0040"
branch_labels = None
depends_on = None


def upgrade():
    op.rename_table("workbench_capability_calls", "workbench_calls")
    op.execute("""ALTER TABLE workbench_calls
      ALTER COLUMN connection_id DROP NOT NULL,
      ADD COLUMN source_asset_id uuid REFERENCES workbench_capability_assets(id),
      ADD COLUMN kind text NOT NULL DEFAULT 'tool' CHECK (kind IN ('method','tool')),
      ADD COLUMN origin text NOT NULL DEFAULT 'managed' CHECK (origin IN ('managed','platform','method')),
      ADD COLUMN call_key text,
      ADD COLUMN parent_call_id uuid,
      ADD COLUMN task_id uuid REFERENCES workbench_tasks(id),
      ADD COLUMN attempt_id uuid REFERENCES workbench_attempts(id),
      ADD COLUMN registration_id text,
      ADD COLUMN registration_digest text,
      ADD CONSTRAINT uq_calls_owner_session_id UNIQUE(owner_actor_id,session_id,id),
      ADD CONSTRAINT fk_calls_parent FOREIGN KEY(owner_actor_id,session_id,parent_call_id)
        REFERENCES workbench_calls(owner_actor_id,session_id,id),
      ADD CONSTRAINT ck_calls_not_self_parent CHECK(parent_call_id IS DISTINCT FROM id)
    """)
    # Preserve the original signed intent/digest as evidence. Old pending approvals
    # cannot execute after cutover; history is not an executable compatibility path.
    op.execute("""UPDATE workbench_calls c SET source_asset_id=c.connection_id,
      connection_id=CASE WHEN a.kind='connection' THEN c.connection_id ELSE NULL END,
      call_key='historical:' || c.id::text,
      status=CASE WHEN c.status='awaiting_approval' THEN 'expired'
                  WHEN c.status='dispatching' THEN 'unknown' ELSE c.status END
      FROM workbench_capability_assets a WHERE a.id=c.connection_id""")
    op.execute("""ALTER TABLE workbench_calls ALTER COLUMN call_key SET NOT NULL,
      ADD CONSTRAINT uq_calls_invocation UNIQUE(owner_actor_id,session_id,call_key)""")
    op.execute("CREATE INDEX ix_calls_parent ON workbench_calls(parent_call_id)")
    op.execute(
        "CREATE UNIQUE INDEX uq_calls_root_task ON workbench_calls(task_id) WHERE kind='method' AND parent_call_id IS NULL"
    )
    op.execute("""UPDATE workbench_capability_selections SET
      runtime=runtime || jsonb_build_object('status','completed','aborted',true),
      updated_at=now() WHERE runtime <> '{}'::jsonb""")


def downgrade():
    # No lossy conversion of method/platform history into the former asset-only table.
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM workbench_calls WHERE origin <> 'managed') THEN
        RAISE EXCEPTION 'shared call history requires forward repair, not downgrade';
      END IF;
    END $$""")
    op.execute("""UPDATE workbench_calls SET connection_id=source_asset_id,
      status=CASE WHEN status='awaiting_approval' THEN 'expired'
                  WHEN status='dispatching' THEN 'unknown' ELSE status END""")
    op.execute("""ALTER TABLE workbench_calls
      DROP CONSTRAINT fk_calls_parent, DROP CONSTRAINT uq_calls_owner_session_id,
      DROP CONSTRAINT ck_calls_not_self_parent, DROP CONSTRAINT uq_calls_invocation,
      DROP COLUMN source_asset_id, DROP COLUMN kind, DROP COLUMN origin,
      DROP COLUMN call_key, DROP COLUMN parent_call_id, DROP COLUMN task_id,
      DROP COLUMN attempt_id, DROP COLUMN registration_id, DROP COLUMN registration_digest,
      ALTER COLUMN connection_id SET NOT NULL""")
    op.rename_table("workbench_calls", "workbench_capability_calls")
