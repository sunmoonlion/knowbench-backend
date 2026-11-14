"""Persist formal admission runs and immutable registration projections."""

from alembic import op

revision = "20261114_0043"
down_revision = "20261114_0042"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workbench_formal_validations (
        run_id uuid PRIMARY KEY,
        owner_actor_id uuid NOT NULL,
        caller text NOT NULL,
        request_id uuid NOT NULL,
        request_digest text NOT NULL,
        request jsonb NOT NULL,
        result jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE workbench_formal_methods (
        method_id uuid NOT NULL,
        version integer NOT NULL CHECK(version > 0),
        owner_actor_id uuid NOT NULL,
        caller text NOT NULL,
        digest text NOT NULL,
        document jsonb NOT NULL,
        run_id uuid NOT NULL REFERENCES workbench_formal_validations(run_id),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(method_id,version)
    )""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM workbench_formal_validations)
          OR EXISTS(SELECT 1 FROM workbench_formal_methods) THEN
          RAISE EXCEPTION 'formal admission evidence requires forward repair';
        END IF;
    END $$""")
    op.drop_table("workbench_formal_methods")
    op.drop_table("workbench_formal_validations")
