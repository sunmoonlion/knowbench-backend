"""Bind script reservations and usage receipts to their root budget entries."""

from alembic import op

revision = "20261120_0049"
down_revision = "20261120_0048"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE workbench_budget_ledger
        ADD COLUMN script_call_id uuid,
        ADD COLUMN script_evidence jsonb,
        ADD CONSTRAINT ck_budget_script_entry CHECK (
          (script_call_id IS NULL AND script_evidence IS NULL) OR
          (script_call_id IS NOT NULL AND script_evidence IS NOT NULL
           AND jsonb_typeof(script_evidence) = 'object'
           AND attempt_id IS NOT NULL AND entry IN ('reserve','consume','release')
           AND amount >= 0))""")
    op.execute("""UPDATE workbench_budget_ledger
        SET script_call_id=(substr(note,49)::jsonb->>'call_id')::uuid,
            script_evidence=substr(note,49)::jsonb->'evidence',
            note=substr(note,49)::jsonb->>'note'
        WHERE left(note,15)='script-cost/v1:' AND substr(note,48,1)=':'
          AND substr(note,16,32)=md5(substr(note,49))""")
    op.execute("""CREATE UNIQUE INDEX uq_budget_script_entry
        ON workbench_budget_ledger(script_call_id, entry)
        WHERE script_call_id IS NOT NULL""")


def downgrade():
    # Keep the complete metering evidence with the original ledger entry on rollback.
    op.execute("""WITH saved AS (
            SELECT id, jsonb_build_object('call_id',script_call_id,
                'evidence',script_evidence,'note',note)::text AS payload
            FROM workbench_budget_ledger WHERE script_call_id IS NOT NULL)
        UPDATE workbench_budget_ledger l
        SET note='script-cost/v1:' || md5(saved.payload) || ':' || saved.payload
        FROM saved WHERE l.id=saved.id""")
    op.execute("""ALTER TABLE workbench_budget_ledger
        DROP COLUMN script_evidence, DROP COLUMN script_call_id""")
