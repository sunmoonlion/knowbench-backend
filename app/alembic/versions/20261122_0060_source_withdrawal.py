"""Keep withdrawn direct source revisions for historical receipts."""

from alembic import op

revision = "20261122_0060"
down_revision = "20261122_0059"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE workbench_registry_sources ADD COLUMN withdrawn_at timestamptz"
    )


def downgrade():
    # Older runtimes treat every stored revision as executable.
    op.execute("LOCK TABLE workbench_registry_sources IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM workbench_registry_sources WHERE withdrawn_at IS NOT NULL) THEN
            RAISE EXCEPTION 'Downgrade would reactivate withdrawn source revisions';
        END IF;
    END $$""")
    op.drop_column("workbench_registry_sources", "withdrawn_at")
