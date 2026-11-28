from __future__ import annotations

import pytest

from core.config import Settings


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "env": "production",
        "casdoor_endpoint": "https://identity.example.test",
        "admin_casdoor_client_id": "tpl-admin-client",
        "admin_casdoor_client_secret": "admin-test-secret",
        "admin_casdoor_redirect_uri": (
            "https://admin.example.test/api/auth/admin/callback"
        ),
        "admin_frontend_base_url": "https://admin.example.test",
        "admin_frontend_allowed_origins": "https://admin.example.test",
        "web_casdoor_client_id": "tpl-web-client",
        "web_casdoor_client_secret": "web-test-secret",
        "web_casdoor_redirect_uri": ("https://web.example.test/api/auth/web/callback"),
        "web_frontend_base_url": "https://web.example.test",
        "web_frontend_allowed_origins": "https://web.example.test",
        "allowed_hosts": "admin.example.test,web.example.test",
    }
    values.update(overrides)
    return Settings(**values)


def test_one_settings_root_exposes_isolated_browser_profiles() -> None:
    settings = production_settings()
    settings.require_browser_identity()
    admin = settings.browser_profile("admin")
    web = settings.browser_profile("web")

    assert admin.client_id != web.client_id
    assert admin.client_secret != web.client_secret
    assert admin.redirect_uri.endswith("/api/auth/admin/callback")
    assert web.redirect_uri.endswith("/api/auth/web/callback")
    assert admin.session_cookie_name == "sunmoonai_knowbench_admin_sid"
    assert web.session_cookie_name == "sunmoonai_knowbench_web_sid"
    assert admin.session_key_prefix != web.session_key_prefix
    assert settings.frontend_origin_list == (
        "https://admin.example.test",
        "https://web.example.test",
    )


def test_database_urls_preserve_driver_ssl_policy() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql://tpl:secret@postgresql:5432/tpl?"
            "sslmode=disable&application_name=tpl"
        ),
        migration_database_url=(
            "postgresql://migrator:secret@postgresql:5432/tpl?sslmode=require"
        ),
    )
    assert settings.database_url == (
        "postgresql+asyncpg://tpl:secret@postgresql:5432/tpl?ssl=disable&application_name=tpl"
    )
    assert settings.migration_url == (
        "postgresql+asyncpg://migrator:secret@postgresql:5432/tpl?ssl=require"
    )


def test_api_requires_both_browser_clients_but_can_validate_one_explicitly() -> None:
    settings = production_settings(web_casdoor_client_secret="")
    settings.require_browser_identity("admin")
    with pytest.raises(ValueError, match="WEB_CASDOOR_CLIENT_SECRET"):
        settings.require_browser_identity()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("admin_frontend_allowed_origins", "*", "wildcard"),
        ("allowed_hosts", "*", "ALLOWED_HOSTS"),
        ("session_cookie_secure", False, "SESSION_COOKIE_SECURE"),
        ("casdoor_verify_ssl", False, "CASDOOR_VERIFY_SSL"),
    ],
)
def test_production_rejects_weak_security(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        production_settings(**{field: value})


def test_redirect_paths_and_origins_are_exact() -> None:
    with pytest.raises(ValueError, match="must end"):
        production_settings(
            admin_casdoor_redirect_uri=(
                "https://admin.example.test/api/auth/web/callback"
            )
        ).require_browser_identity()
    with pytest.raises(ValueError, match="matching FRONTEND_BASE_URL"):
        production_settings(
            web_casdoor_redirect_uri=(
                "https://other.example.test/api/auth/web/callback"
            )
        ).require_browser_identity()


def test_web_return_to_allowlist_and_admin_root_policy_are_explicit() -> None:
    settings = production_settings()
    assert settings.browser_profile("admin").allowed_return_paths == ("/",)
    assert settings.browser_profile("web").allowed_return_paths == (
        "/zh-CN/dashboard",
        "/en/dashboard",
        "/zh-CN/login",
        "/en/login",
    )
    with pytest.raises(ValueError, match="must match"):
        production_settings(
            web_auth_default_return_to="/private",
            web_auth_allowed_return_paths="/zh-CN/dashboard",
        )


def test_reference_adapter_and_weak_algorithms_fail_closed() -> None:
    with pytest.raises(ValueError, match="REFERENCE_INTERACTION_ENABLED"):
        production_settings(reference_interaction_enabled=True)
    with pytest.raises(ValueError, match="asymmetric"):
        Settings(_env_file=None, auth_allowed_algorithms="HS256")


def test_same_backend_downstream_target_is_rejected() -> None:
    settings = production_settings(
        downstream_base_url="https://web.example.test",
        downstream_client_id="service-client",
        downstream_client_secret="test-secret",
        downstream_scope="other:read",
    )
    with pytest.raises(ValueError, match="cannot target this Backend"):
        settings.require_downstream_identity()


def test_service_identity_bindings_are_explicit_and_fail_closed() -> None:
    settings = production_settings(
        service_auth_audience="knowbase-internal",
        service_auth_subject_bindings_json=(
            '{"knowbench-agent-worker":["knowbase:retrieve"]}'
        ),
    )
    settings.require_service_identity()
    assert settings.service_auth_subject_bindings == {
        "knowbench-agent-worker": frozenset({"knowbase:retrieve"})
    }

    with pytest.raises(ValueError, match="valid JSON"):
        production_settings(
            service_auth_audience="knowbase-internal",
            service_auth_subject_bindings_json="not-json",
        ).require_service_identity()
    with pytest.raises(ValueError, match="cannot be empty"):
        production_settings(
            service_auth_audience="knowbase-internal",
            service_auth_subject_bindings_json="{}",
        ).require_service_identity()
