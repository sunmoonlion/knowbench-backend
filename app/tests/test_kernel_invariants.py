from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_split_backend_shortcuts_are_absent() -> None:
    auth_service = (ROOT / "app/application/services/auth_service.py").read_text()
    api = (ROOT / "app/bootstrap/api.py").read_text()
    routes = (ROOT / "app/interfaces/http/routes.py").read_text()

    assert "AdminAuthService" not in auth_service
    assert "WebAuthService" not in auth_service
    assert "ADMIN_BACKEND_INTERNAL_URL" not in api
    assert "WEB_BACKEND_INTERNAL_URL" not in api
    assert 'allow_origins=["*"]' not in api
    assert "admin_auth_router" in routes and "web_auth_router" in routes


def test_interface_partition_and_dependency_direction_are_explicit() -> None:
    assert (ROOT / "app/interfaces/http/admin/auth.py").is_file()
    assert (ROOT / "app/interfaces/http/web/auth.py").is_file()
    assert (ROOT / "app/interfaces/http/web/interactions.py").is_file()
    assert (ROOT / "app/bootstrap/api.py").is_file()
    assert (ROOT / "app/bootstrap/worker.py").is_file()
    assert (ROOT / "app/bootstrap/scheduler.py").is_file()
    assert (ROOT / "app/bootstrap/migration.py").is_file()
    assert (ROOT / "app/infrastructure/repositories/outbox.py").is_file()
    assert (ROOT / "app/infrastructure/security/service_identity.py").is_file()

    application_sources = "\n".join(
        path.read_text() for path in (ROOT / "app/application").rglob("*.py")
    )
    assert "app.interfaces" not in application_sources


def test_one_linear_canonical_migration_chain() -> None:
    revisions = sorted(
        path
        for path in (ROOT / "alembic/versions").glob("*.py")
        if path.name != "__init__.py"
    )
    assert [path.name for path in revisions] == [
        "20260708_0001_agent_phase0.py",
        "20260712_0002_auth_identity.py",
        "20260729_0003_agent_pilot.py",
        "20260809_0004_outbox_primitives.py",
        "20260811_0005_uuid_defaults.py",
        "20260910_0006_agent_reliability.py",
        "20260911_0007_durable_delivery.py",
        "20260924_0008_workbench.py",
        "20260924_0009_workbench_prefs.py",
        "20260925_0010_workbench_relay_identity.py",
        "20260925_0011_workbench_sandbox_leases.py",
        "20260929_0012_workbench_projects.py",
        "20261007_0013_workbench_library.py",
        "20261009_0014_agent_onboarding.py",
        "20261012_0015_tool_approvals.py",
        "20261012_0016_memories.py",
        "20261012_0017_session_archive.py",
        "20261012_0018_settings_three_kinds.py",
        "20261016_0019_knowbot_pins.py",
        "20261017_0020_knowbot_test_runs.py",
        "20261019_0021_test_case_outputs.py",
    ]
    contents = [path.read_text() for path in revisions]
    assert sum("down_revision = None" in content for content in contents) == 1
    assert 'down_revision = "20260708_0001"' in contents[1]
    assert 'down_revision = "20260712_0002"' in contents[2]
    assert 'down_revision = "20260729_0003"' in contents[3]
    assert 'down_revision = "20260809_0004"' in contents[4]
    assert 'down_revision = "20260811_0005"' in contents[5]
    assert 'down_revision = "20260910_0006"' in contents[6]
    assert 'down_revision = "20260911_0007"' in contents[7]
    assert 'down_revision = "20260924_0008"' in contents[8]
    assert 'down_revision = "20260924_0009"' in contents[9]
    assert 'down_revision = "20260925_0010"' in contents[10]
    assert 'down_revision = "20260925_0011"' in contents[11]
    assert 'down_revision = "20260929_0012"' in contents[12]
    assert 'down_revision = "20261007_0013"' in contents[13]


def test_uuid_mixin_has_client_and_database_defaults() -> None:
    from app.infrastructure.models.auth import AuthUser

    identifier = AuthUser.__table__.c.id
    assert identifier.default is not None
    assert identifier.server_default is not None


def test_runtime_image_context_excludes_credentials_and_tests() -> None:
    dockerignore = (ROOT.parent / ".dockerignore").read_text().splitlines()
    assert "app/.env" in dockerignore
    assert "app/.env.*" in dockerignore
    assert "app/tests" in dockerignore


def test_package_version_matches_the_formal_release() -> None:
    """源码版本必须等于正式发布版本 2.0.0。

    2026-08-13 的 Architecture v2 正式发布采用 exact-digest-alias 晋升：
    不重建镜像，给已通过 R7 门禁的 digest 打 `2.0.0` 别名
    （历史 R7 发布清单按 k8s:sunmoonai/docs/legacy-backlog/verification-index.md 查询）。
    本测试钉住源码侧的版本字符串，使 `/api/version` 与该别名一致。

    本测试的前身 `test_candidate_does_not_claim_the_formal_release` 禁止源码写
    `2.0.0`，那是 2026-08-01 重构期的护栏；发布已于 8-13 完成，护栏反向。
    """
    project = (ROOT / "pyproject.toml").read_text()
    assert 'version = "2.0.0"' in project
    assert "dev0" not in project

    # uv.lock 与 pyproject 必须同步，否则 Dockerfile 的 `uv sync --frozen` 会失败
    name = re.search(r'^name = "(.+?)"', project, re.M).group(1)
    lock = (ROOT / "uv.lock").read_text()
    assert re.search(rf'name = "{re.escape(name)}"\nversion = "2\.0\.0"\n', lock)

    # 版本只能来自包元数据，不得在代码里硬写
    api = (ROOT / "app/bootstrap/api.py").read_text()
    assert 'version="2.0.0"' not in api
    assert f'version("{name}")' in api
