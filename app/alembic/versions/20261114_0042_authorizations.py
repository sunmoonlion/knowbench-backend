"""Persist method authority separately from call evidence and dispatch attempts."""

from alembic import op

revision = "20261114_0042"
down_revision = "20261114_0041"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workbench_method_authorizations (
        call_id uuid PRIMARY KEY,
        owner_actor_id uuid NOT NULL,
        session_id uuid NOT NULL,
        revision integer NOT NULL CHECK(revision > 0),
        document jsonb NOT NULL,
        confirmed_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(owner_actor_id,session_id,call_id)
          REFERENCES workbench_calls(owner_actor_id,session_id,id)
    )""")
    # Existing confirmations did not include a delegation boundary. Retain the
    # selection and its historical evidence, but require confirmation under the
    # new semantics before any method can dispatch. No old grant fallback.
    op.execute("""UPDATE workbench_capability_selections SET
        runtime=runtime || jsonb_build_object('status','completed','aborted',true),
        updated_at=now() WHERE runtime <> '{}'::jsonb""")
    op.execute("""UPDATE workbench_calls SET status='expired',updated_at=now()
        WHERE status='awaiting_approval'""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM workbench_method_authorizations) THEN
          RAISE EXCEPTION 'method authorizations require forward repair';
        END IF;
    END $$""")
    op.drop_table("workbench_method_authorizations")
