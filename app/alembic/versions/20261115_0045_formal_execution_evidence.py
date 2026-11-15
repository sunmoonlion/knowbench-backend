"""Retain immutable formal test evidence beyond disposable task cleanup."""

from alembic import op

revision = "20261115_0045"
down_revision = "20261115_0044"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE workbench_formal_validations ADD COLUMN execution_evidence jsonb"
    )
    _sources(True)


def _sources(formal):
    op.drop_constraint("ck_workbench_tasks_pack_source", "workbench_tasks")
    sources = (
        "'knowmart', 'knowlab-test', 'formal-test'"
        if formal
        else "'knowmart', 'knowlab-test'"
    )
    op.create_check_constraint(
        "ck_workbench_tasks_pack_source",
        "workbench_tasks",
        "(pack_source = 'builtin' and pack_snapshot is null and pack_digest is null)"
        f" or (pack_source in ({sources}) and pack_snapshot is not null and pack_digest is not null)",
    )


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM workbench_formal_validations WHERE execution_evidence IS NOT NULL)
        OR EXISTS(SELECT 1 FROM workbench_tasks WHERE pack_source='formal-test') THEN
        RAISE EXCEPTION 'formal execution evidence requires forward repair';
      END IF;
    END $$""")
    _sources(False)
    op.drop_column("workbench_formal_validations", "execution_evidence")
