"""项目、对话的种类（PRD/apps/investment.md）：工作区 → 项目 → 对话（聊天 / 工作）。

只加不删。已有的会话都记为「工作」，并各自归入一个项目：目录取原来的 project_root。
"""

import json
import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "20260929_0012"
down_revision = "20260925_0011"
branch_labels = None
depends_on = None

TERMINAL = "('SUCCEEDED','REJECTED','FAILED','CANCELLED')"
_WINDOWS = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


def _split(directory: str, roots: list[str]) -> tuple[str, str]:
    """迁移当时的规则，冻结在这里：目录落在哪个工作区下。落不进任何一个，它自己就是工作区。"""
    best: tuple[str, str] | None = None
    for root in roots:
        if not isinstance(root, str) or not root:
            continue
        windows = bool(_WINDOWS.match(root))
        # Compare Windows separators case-insensitively while preserving POSIX names.
        trimmed = root.rstrip("\\/" if windows else "/") or root
        left = directory.replace("\\", "/").lower() if windows else directory
        right = trimmed.replace("\\", "/").lower() if windows else trimmed
        prefix = right.rstrip("/") + "/"
        if left.rstrip("/") == right.rstrip("/"):
            relative = ""
        elif left.startswith(prefix):
            separators = "\\/" if windows else "/"
            relative = directory[len(trimmed.rstrip(separators)) + 1:]
            if windows:
                relative = relative.replace("\\", "/")
            relative = relative.rstrip("/")
        else:
            continue
        if best is None or len(trimmed) > len(best[0].rstrip("\\/")):
            best = (root, relative)
    return best or (directory, "")


def upgrade():
    op.create_table(
        "workbench_projects",
        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column("owner_actor_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "environment_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workbench_environments.id"),
            nullable=False,
        ),
        sa.Column("workspace_root", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False, server_default=""),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_workbench_projects_owner", "workbench_projects", ["owner_actor_id"]
    )
    # 同一个人、同一台机器、同一个目录，只有一个没归档的项目
    op.create_index(
        "uq_workbench_projects_live_dir",
        "workbench_projects",
        ["owner_actor_id", "environment_id", "workspace_root", "path"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.add_column(
        "workbench_sessions",
        sa.Column("kind", sa.String(8), nullable=False, server_default="work"),
    )
    op.add_column(
        "workbench_sessions",
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workbench_projects.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "workbench_sessions", sa.Column("title", sa.String(200), nullable=True)
    )
    op.alter_column("workbench_sessions", "environment_id", nullable=True)
    op.alter_column("workbench_sessions", "project_root", nullable=True)
    op.create_index(
        "ix_workbench_sessions_project", "workbench_sessions", ["project_id"]
    )

    op.add_column(
        "workbench_tasks",
        sa.Column(
            "project_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("workbench_projects.id"),
            nullable=True,
        ),
    )

    connection = op.get_bind()
    sessions = connection.execute(
        sa.text(
            "SELECT s.id, s.owner_actor_id, s.environment_id, s.project_root, e.roots "
            "FROM workbench_sessions s JOIN workbench_environments e "
            "ON e.id = s.environment_id ORDER BY s.created_at"
        )
    ).all()
    projects: dict[tuple, str] = {}
    for session_id, owner, environment, directory, roots in sessions:
        if isinstance(roots, str):
            roots = json.loads(roots)
        root, relative = _split(directory, list(roots or []))
        key = (str(owner), str(environment), root, relative)
        if key not in projects:
            source = relative or root.rstrip("\\/")
            title = (re.split(r"[\\/]", source)[-1] or source or "项目")[:200]
            projects[key] = str(
                connection.execute(
                    sa.text(
                        "INSERT INTO workbench_projects (owner_actor_id, environment_id,"
                        " workspace_root, path, title) VALUES (:o, :e, :r, :p, :t)"
                        " RETURNING id"
                    ),
                    {"o": owner, "e": environment, "r": root, "p": relative, "t": title},
                ).scalar_one()
            )
        connection.execute(
            sa.text("UPDATE workbench_sessions SET project_id = :p WHERE id = :s"),
            {"p": projects[key], "s": session_id},
        )
    connection.execute(
        sa.text(
            "UPDATE workbench_tasks t SET project_id = s.project_id "
            "FROM workbench_sessions s WHERE s.id = t.session_id"
        )
    )

    op.create_check_constraint(
        "ck_workbench_sessions_kind", "workbench_sessions", "kind in ('chat','work')"
    )
    op.create_check_constraint(
        "ck_workbench_sessions_work_in_project",
        "workbench_sessions",
        "kind <> 'work' OR project_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_workbench_sessions_project_has_directory",
        "workbench_sessions",
        "project_id IS NULL OR (environment_id IS NOT NULL AND project_root IS NOT NULL)",
    )
    # 一个项目同一时刻只有一个进行中的委托（所有者 2026-09-29 定）
    op.create_index(
        "uq_workbench_tasks_project_active",
        "workbench_tasks",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(f"project_id IS NOT NULL AND state NOT IN {TERMINAL}"),
    )


def downgrade():
    op.drop_index("uq_workbench_tasks_project_active", "workbench_tasks")
    op.drop_constraint(
        "ck_workbench_sessions_project_has_directory", "workbench_sessions"
    )
    op.drop_constraint("ck_workbench_sessions_work_in_project", "workbench_sessions")
    op.drop_constraint("ck_workbench_sessions_kind", "workbench_sessions")
    op.drop_column("workbench_tasks", "project_id")
    op.drop_index("ix_workbench_sessions_project", "workbench_sessions")
    # 不属于项目的聊天没有目录，回不到「目录必填」的旧样子：降级时删掉它们
    op.execute("DELETE FROM workbench_sessions WHERE project_id IS NULL")
    op.alter_column("workbench_sessions", "project_root", nullable=False)
    op.alter_column("workbench_sessions", "environment_id", nullable=False)
    op.drop_column("workbench_sessions", "title")
    op.drop_column("workbench_sessions", "project_id")
    op.drop_column("workbench_sessions", "kind")
    op.drop_index("uq_workbench_projects_live_dir", "workbench_projects")
    op.drop_index("ix_workbench_projects_owner", "workbench_projects")
    op.drop_table("workbench_projects")
