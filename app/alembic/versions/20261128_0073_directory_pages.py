"""A short-lived, owner-scoped index for paged planning directory results."""

from alembic import op

revision = "20261128_0073"
down_revision = "20261128_0072"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workbench_directory_pages (
        owner_id uuid NOT NULL, kind text NOT NULL, query_digest text NOT NULL,
        entries jsonb NOT NULL, counts jsonb NOT NULL, expires_at timestamptz NOT NULL,
        PRIMARY KEY(owner_id,kind))""")
    op.execute(
        "CREATE INDEX ix_workbench_directory_pages_expiry ON workbench_directory_pages(expires_at)"
    )


def downgrade():
    op.drop_table("workbench_directory_pages")
