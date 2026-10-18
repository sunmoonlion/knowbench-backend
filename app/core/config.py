from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.cross_app import Source, Target, parse_sources, parse_targets
from app.domain.workbench.pricing import DEFAULT_PRICES_JSON, PriceList, parse_prices


class AgentDownload(BaseModel):
    """Single operator-owned release descriptor; never supplied by the browser."""

    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["external", "object-storage"] = "external"
    object_key: str | None = Field(default=None, max_length=256)
    url: str = Field(min_length=1, max_length=2048)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    zip_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    codex_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    size_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def hosting(self) -> AgentDownload:
        import re

        if self.mode == "external":
            if self.object_key is not None:
                raise ValueError("external downloads cannot select an object")
        elif (
            not self.object_key
            or not re.fullmatch(
                r"windows-x64/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\.zip", self.object_key
            )
            or ".." in self.object_key
        ):
            raise ValueError("invalid release object key")
        return self

    def public_descriptor(self) -> dict:
        return self.model_dump(exclude={"mode", "object_key"})

    @field_validator("url")
    @classmethod
    def public_https(cls, value: str) -> str:
        u = urlsplit(value)
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value
        ):
            raise ValueError("agent download must be a credential-free HTTPS URL")
        _ = u.port
        return value


class AgentReleaseStorage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint: str
    region: str = "us-east-1"
    ca_file: str
    access_key: SecretStr
    secret_key: SecretStr

    @field_validator("endpoint")
    @classmethod
    def endpoint_https(cls, value: str) -> str:
        AgentDownload.public_https(value)
        if urlsplit(value).path not in ("", "/"):
            raise ValueError("storage endpoint cannot contain a path")
        return value.rstrip("/")

    @field_validator("ca_file", "region", "access_key", "secret_key")
    @classmethod
    def not_empty(cls, value):
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw or not raw.strip():
            raise ValueError("empty release storage setting")
        return value


BrowserSurface = Literal["admin", "web"]


@dataclass(frozen=True, slots=True)
class BrowserSurfaceProfile:
    """Immutable security boundary for one browser surface."""

    surface: BrowserSurface
    client_id: str
    client_secret: str
    redirect_uri: str
    application: str
    frontend_base_url: str
    frontend_origins: tuple[str, ...]
    policy_version: str
    role_allowlist: frozenset[str]
    scope_allowlist: frozenset[str]
    default_return_to: str
    allowed_return_paths: tuple[str, ...]
    required_scopes: tuple[str, ...]
    session_cookie_name: str
    transaction_cookie_name: str
    session_key_prefix: str
    transaction_key_prefix: str


class Settings(BaseSettings):
    """One App Backend configuration with two isolated browser identities."""

    env: str = "development"
    log_level: str = "INFO"
    service_name: str = "knowbench-backend"
    deployment_id: str = "local"
    app_slug: str = "knowbench"

    # 跨应用跳转（app/domain/cross_app.py）。都可不配：不配就是不带人来、也不带人去。
    # 哪些应用可以把用户带到这里，各自的回跳地址。回跳地址只从这里取，从不从链接里取。
    #   {"knowbench": {"return_url": "https://…/zh-CN/workbench?ref={ref}"},
    #    "knowbase": {}}
    cross_app_sources_json: str = "{}"
    # 这个应用会把用户带去哪些应用，各自网页端的地址。
    #   {"knowfeed": {"web_base_url": "https://knowfeed.example"}}
    cross_app_targets_json: str = "{}"

    database_url: str = (
        "postgresql+asyncpg://knowbench:knowbench@localhost:5432/knowbench"
    )
    migration_database_url: str | None = None

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_user: str | None = None
    redis_password: str | None = None

    # The provider transport is shared; each browser surface has its own
    # registered OAuth client below.
    casdoor_endpoint: str = ""
    casdoor_discovery_url: str | None = None
    casdoor_backchannel_endpoint: str | None = None
    # 管理端只认这个组织（Casdoor built-in，全局管理员所在）；网页端认下面的列表。
    # 两者分开，是为了让普通用户（单独的用户组织）永远拿不到管理端会话。
    casdoor_organization: str = "built-in"
    web_casdoor_organizations: str = "built-in"
    casdoor_verify_ssl: bool = True

    admin_casdoor_client_id: str = ""
    admin_casdoor_client_secret: str = ""
    admin_casdoor_redirect_uri: str = ""
    admin_casdoor_application: str = "sunmoonai-knowbench-admin"
    admin_auth_policy_version: str = "knowbench-admin-v2"
    admin_auth_role_allowlist: str = ""
    admin_auth_scope_allowlist: str = ""
    admin_frontend_base_url: str = "http://localhost:5173"
    admin_frontend_allowed_origins: str | None = None
    admin_auth_default_return_to: str = "/"
    admin_auth_allowed_return_paths: str = "/"

    web_casdoor_client_id: str = ""
    web_casdoor_client_secret: str = ""
    web_casdoor_redirect_uri: str = ""
    web_casdoor_application: str = "sunmoonai-knowbench-web"
    web_auth_policy_version: str = "knowbench-web-v2"
    web_auth_role_allowlist: str = ""
    web_auth_scope_allowlist: str = ""
    web_frontend_base_url: str = "http://localhost:3000"
    web_frontend_allowed_origins: str | None = None
    web_auth_default_return_to: str = "/zh-CN/dashboard"
    web_auth_allowed_return_paths: str = (
        "/zh-CN/dashboard,/en/dashboard,/zh-CN/login,/en/login"
    )
    web_frontend_default_locale: str = "zh-CN"

    auth_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    auth_transaction_ttl_seconds: int = Field(default=300, ge=60, le=900)
    auth_discovery_cache_seconds: int = Field(default=300, ge=1, le=3600)
    auth_jwks_cache_seconds: int = Field(default=300, ge=1, le=3600)
    auth_clock_skew_seconds: int = Field(default=30, ge=0, le=120)
    auth_allowed_algorithms: str = "RS256,ES256"
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    session_cookie_secure: bool | None = None

    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    reference_interaction_enabled: bool = False

    # Cross-App/provider boundary only. A same-Backend URL is rejected.
    downstream_base_url: str = ""
    downstream_client_id: str = ""
    downstream_client_secret: str = ""
    downstream_scope: str = ""
    downstream_allowed_path_prefixes: str = "/api/internal/v1"
    downstream_verify_ssl: bool = True
    downstream_http_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    # Provider-signed workload identity accepted by /api/internal dependencies.
    # The JSON object maps an exact token subject to its maximum allowed scopes.
    service_auth_audience: str = ""
    service_auth_subject_bindings_json: str = "{}"

    celery_broker_url: str | None = Field(
        default=None, validation_alias="CELERY_BROKER_URL"
    )
    celery_queue: str = Field(
        default="knowbench.default",
        validation_alias=AliasChoices("CELERY_QUEUE", "CELERY_TASK_DEFAULT_QUEUE"),
    )
    celery_result_backend: str | None = Field(
        default=None, validation_alias="CELERY_RESULT_BACKEND"
    )
    # Opt-in only after a privileged provisioner verifies the durable binding.
    celery_task_topology_predeclared: bool = Field(
        default=False, validation_alias="CELERY_TASK_TOPOLOGY_PREDECLARED"
    )

    # 工作台（0001-workbench）：网页接口与 runner 角色的开关
    workbench_enabled: bool = Field(default=False, validation_alias="WORKBENCH_ENABLED")
    workbench_redis_key_prefix: str = Field(
        default="knowbench:workbench", validation_alias="WORKBENCH_REDIS_KEY_PREFIX"
    )
    workbench_runner_id: str = Field(
        default="runner-local", validation_alias="WORKBENCH_RUNNER_ID"
    )
    workbench_poll_seconds: float = Field(
        default=1.0, gt=0, le=30, validation_alias="WORKBENCH_POLL_SECONDS"
    )
    workbench_environment_key: str = Field(
        default="user-pc", validation_alias="WORKBENCH_ENVIRONMENT_KEY"
    )
    # BYOK 凭据登记的对称密钥（Fernet，urlsafe base64）；未配置时凭据接口返回 503
    workbench_credential_key: str | None = Field(
        default=None, validation_alias="WORKBENCH_CREDENTIAL_KEY"
    )
    # 沙箱供给器（0003 D9）与会合点管理通道（0004）：内网地址与共享令牌；未配置时相关接口 503
    workbench_provisioner_url: str | None = Field(
        default=None, validation_alias="WORKBENCH_PROVISIONER_URL"
    )
    workbench_provisioner_token: str | None = Field(
        default=None, validation_alias="WORKBENCH_PROVISIONER_TOKEN"
    )
    workbench_relay_admin_url: str | None = Field(
        default=None, validation_alias="WORKBENCH_RELAY_ADMIN_URL"
    )
    workbench_relay_admin_token: str | None = Field(
        default=None, validation_alias="WORKBENCH_RELAY_ADMIN_TOKEN"
    )
    # 我的机器：runner 每隔这么多秒问一次会合点谁在线，登记机器、置在线离线；0 = 不同步（没配会合点管理通道时也不同步）
    workbench_machine_sync_seconds: float = Field(
        default=10.0, ge=0, le=300, validation_alias="WORKBENCH_MACHINE_SYNC_SECONDS"
    )
    # 作为运行方跑分身测试（SDD 0027 §3.4）：测试账号的 Casdoor 用户 id、测试电脑上的
    # 工作区根目录、每月测试费用上限（所有者定 1000 元）。前两项没配，测试接口回 503
    knowbot_test_subject: str | None = Field(
        default=None, validation_alias="KNOWBOT_TEST_SUBJECT"
    )
    knowbot_test_workspace: str | None = Field(
        default=None, validation_alias="KNOWBOT_TEST_WORKSPACE"
    )
    knowbot_test_monthly_limit: Decimal = Field(
        default=Decimal("1000"), gt=0, validation_alias="KNOWBOT_TEST_MONTHLY_LIMIT"
    )
    knowbot_test_poll_seconds: float = Field(
        default=5.0, ge=1, le=300, validation_alias="KNOWBOT_TEST_POLL_SECONDS"
    )
    # 每次测试用自己的目录（账 129）：要测试电脑的本地代理能经服务流建、删 runs/ 下的目录。
    # 0.5.0 的服务流只读（service.ts 只认读的三个方法），所以默认关、在共用的工作区里跑；
    # 测试电脑换上支持的代理（0.5.1）再打开（所有者 2026-10-18 选甲）
    knowbot_test_isolate: bool = Field(
        default=False, validation_alias="KNOWBOT_TEST_ISOLATE"
    )
    # D10：签发代理/沙箱/知识令牌的 ES256 私钥（PEM）；未配置时退回不透明随机令牌（会合点静态表/管理通道）
    workbench_token_signing_key: str | None = Field(
        default=None, validation_alias="WORKBENCH_TOKEN_SIGNING_KEY"
    )
    # Unset means unavailable. Hosting is chosen by the remote operator, not the client.
    workbench_agent_download: AgentDownload | None = Field(
        default=None, validation_alias="WORKBENCH_AGENT_DOWNLOAD"
    )
    workbench_agent_release_storage: AgentReleaseStorage | None = Field(
        default=None, validation_alias="WORKBENCH_AGENT_RELEASE_STORAGE", repr=False
    )
    workbench_agent_pairing_hmac_key: SecretStr | None = Field(
        default=None, validation_alias="WORKBENCH_AGENT_PAIRING_HMAC_KEY", repr=False
    )
    workbench_agent_install_script_template: str | None = Field(
        default=None,
        validation_alias="WORKBENCH_AGENT_INSTALL_SCRIPT_TEMPLATE",
        repr=False,
    )
    # Only these ingress peer CIDRs may supply the rightmost X-Forwarded-For address.
    workbench_trusted_proxy_cidrs: str = Field(
        default="", validation_alias="WORKBENCH_TRUSTED_PROXY_CIDRS", repr=False
    )
    workbench_token_issuer: str = Field(
        default="sunmoon-workbench", validation_alias="WORKBENCH_TOKEN_ISSUER"
    )
    workbench_relay_public_url: str = Field(
        default="wss://edge.sunmoonai.com/relay",
        validation_alias="WORKBENCH_RELAY_PUBLIC_URL",
    )
    # F-SBX-08：全局在跑沙箱上限（自助注册的配套闸）；0 = 不设上限
    workbench_sandbox_global_limit: int = Field(
        default=20, ge=0, validation_alias="WORKBENCH_SANDBOX_GLOBAL_LIMIT"
    )
    workbench_sandbox_model_provider: str = Field(
        default="", validation_alias="WORKBENCH_SANDBOX_MODEL_PROVIDER"
    )
    workbench_sandbox_model: str = Field(
        default="", validation_alias="WORKBENCH_SANDBOX_MODEL"
    )
    workbench_sandbox_provider_base_url: str = Field(
        default="", validation_alias="WORKBENCH_SANDBOX_PROVIDER_BASE_URL"
    )

    # 专家读本项目别的对话与底稿的工具服务（PRD/apps/investment.md 7.4）。
    # 沙箱里的 Codex 配好这个服务之后才打开：打开后专家每一步的说明里会提到这组工具
    workbench_records_mcp_enabled: bool = Field(
        default=False, validation_alias="WORKBENCH_RECORDS_MCP_ENABLED"
    )
    workbench_records_mcp_url: str = Field(
        default="", validation_alias="WORKBENCH_RECORDS_MCP_URL"
    )
    workbench_records_mcp_rate_per_minute: int = Field(
        default=60, ge=1, validation_alias="WORKBENCH_RECORDS_MCP_RATE_PER_MINUTE"
    )

    @field_validator("cross_app_sources_json")
    @classmethod
    def _validate_cross_app_sources(cls, value: str) -> str:
        parse_sources(value)
        return value

    @field_validator("cross_app_targets_json")
    @classmethod
    def _validate_cross_app_targets(cls, value: str) -> str:
        parse_targets(value)
        return value

    def cross_app_sources(self) -> dict[str, Source]:
        return parse_sources(self.cross_app_sources_json)

    def cross_app_targets(self) -> dict[str, Target]:
        return parse_targets(self.cross_app_targets_json)

    # 模型的价目表：每百万 token 多少钱（输入、命中缓存的输入、写进缓存的输入、输出）。
    # 页面上实时显示的金额按它估算。不配就用 app/domain/workbench/pricing.py 里那一份
    # （Kimi 开放平台 2026-10-04 的公开价）。模型调价、换模型，改这一项，不用改代码。
    workbench_model_prices_json: str = Field(
        default=DEFAULT_PRICES_JSON, validation_alias="WORKBENCH_MODEL_PRICES_JSON"
    )

    @field_validator("workbench_model_prices_json")
    @classmethod
    def _validate_model_prices(cls, value: str) -> str:
        parse_prices(value)
        return value

    def workbench_model_prices(self) -> PriceList:
        return parse_prices(self.workbench_model_prices_json)

    @field_validator("database_url", "migration_database_url", mode="before")
    @classmethod
    def normalize_postgres_url(cls, value: str | None) -> str | None:
        if not isinstance(value, str) or not (
            value.startswith("postgresql://")
            or value.startswith("postgresql+asyncpg://")
        ):
            return value
        normalized = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        parts = urlsplit(normalized)
        query = [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key != "sslmode"
        ]
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )

    @field_validator("workbench_trusted_proxy_cidrs")
    @classmethod
    def trusted_proxy_networks_are_valid(cls, value: str) -> str:
        import ipaddress

        for item in value.split(","):
            if item.strip():
                ipaddress.ip_network(item.strip(), strict=False)
        return value

    @model_validator(mode="after")
    def validate_base_security(self) -> Settings:
        release = self.workbench_agent_download
        if release and release.mode == "object-storage":
            if self.workbench_agent_release_storage is None:
                raise ValueError(
                    "object-storage downloads need read-only storage credentials"
                )
            if (
                release.url
                != self.web_frontend_base_url.rstrip("/")
                + "/api/workbench/agent/package"
            ):
                raise ValueError(
                    "forwarded download URL must be the Web same-origin package endpoint"
                )
        if not self.deployment_id.strip():
            raise ValueError("DEPLOYMENT_ID cannot be empty")
        if not self.app_slug or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for char in self.app_slug
        ):
            raise ValueError("APP_SLUG must use lowercase letters, digits and hyphens")
        if any(item == "*" for item in self._split_csv(self.allowed_hosts)):
            if self.is_production:
                raise ValueError("ALLOWED_HOSTS cannot use wildcard in production")
        if not self.allowed_host_list:
            raise ValueError("ALLOWED_HOSTS cannot be empty")
        if self.is_production and not self.casdoor_verify_ssl:
            raise ValueError("CASDOOR_VERIFY_SSL must be true in production")
        if self.is_production and self.session_cookie_secure is False:
            raise ValueError("SESSION_COOKIE_SECURE cannot be false in production")
        if self.is_production and self.reference_interaction_enabled:
            raise ValueError(
                "REFERENCE_INTERACTION_ENABLED cannot be true in production"
            )
        for surface in ("admin", "web"):
            profile = self.browser_profile(surface)
            if self.is_production and any(
                urlsplit(origin).scheme != "https"
                for origin in profile.frontend_origins
            ):
                raise ValueError(
                    f"{surface.upper()}_FRONTEND_ALLOWED_ORIGINS must use HTTPS "
                    "in production"
                )
        if not self.web_frontend_default_locale or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for char in self.web_frontend_default_locale
        ):
            raise ValueError("WEB_FRONTEND_DEFAULT_LOCALE is invalid")
        _ = self.auth_allowed_algorithm_list
        return self

    @staticmethod
    def _split_csv(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @staticmethod
    def _strict_origin(value: str, *, field: str) -> str:
        if value == "*":
            raise ValueError(f"{field} cannot contain wildcard origin")
        try:
            parsed = urlsplit(value)
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError as exc:
            raise ValueError(f"{field} contains an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{field} must contain origin-only HTTP(S) URLs")
        return f"{parsed.scheme}://{parsed.hostname}{port}"

    @staticmethod
    def _validate_relative_path(value: str, *, field: str) -> str:
        if (
            not value.startswith("/")
            or value.startswith("//")
            or "\\" in value
            or "\x00" in value
            or len(value) > 1024
        ):
            raise ValueError(f"{field} must be a safe relative path")
        return value

    @staticmethod
    def _path_is_allowed(value: str, allowed: str) -> bool:
        path = urlsplit(value).path
        if allowed == "/":
            return path.startswith("/")
        return path == allowed or path.startswith(f"{allowed.rstrip('/')}/")

    @property
    def is_production(self) -> bool:
        return self.env not in {"development", "test"}

    @property
    def migration_url(self) -> str:
        return self.migration_database_url or self.database_url

    @property
    def casdoor_discovery_endpoint(self) -> str:
        if self.casdoor_discovery_url:
            return self.casdoor_discovery_url
        if not self.casdoor_endpoint:
            return ""
        return f"{self.casdoor_endpoint.rstrip('/')}/.well-known/openid-configuration"

    @property
    def auth_allowed_algorithm_list(self) -> tuple[str, ...]:
        values = self._split_csv(self.auth_allowed_algorithms)
        if not values:
            raise ValueError("AUTH_ALLOWED_ALGORITHMS cannot be empty")
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if set(values) - allowed:
            raise ValueError(
                "AUTH_ALLOWED_ALGORITHMS must contain only asymmetric algorithms"
            )
        return values

    @property
    def allowed_host_list(self) -> tuple[str, ...]:
        return self._split_csv(self.allowed_hosts)

    @property
    def auth_cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production

    def browser_profile(self, surface: BrowserSurface) -> BrowserSurfaceProfile:
        prefix = surface
        frontend_base = self._strict_origin(
            str(getattr(self, f"{prefix}_frontend_base_url")),
            field=f"{prefix.upper()}_FRONTEND_BASE_URL",
        )
        raw_origins = (
            getattr(self, f"{prefix}_frontend_allowed_origins") or frontend_base
        )
        origins = tuple(
            dict.fromkeys(
                self._strict_origin(
                    item,
                    field=f"{prefix.upper()}_FRONTEND_ALLOWED_ORIGINS",
                )
                for item in self._split_csv(raw_origins)
            )
        )
        if not origins:
            raise ValueError(
                f"{prefix.upper()}_FRONTEND_ALLOWED_ORIGINS cannot be empty"
            )
        default_return_to = self._validate_relative_path(
            str(getattr(self, f"{prefix}_auth_default_return_to")),
            field=f"{prefix.upper()}_AUTH_DEFAULT_RETURN_TO",
        )
        allowed_return_paths = tuple(
            self._validate_relative_path(
                item,
                field=f"{prefix.upper()}_AUTH_ALLOWED_RETURN_PATHS",
            )
            for item in self._split_csv(
                str(getattr(self, f"{prefix}_auth_allowed_return_paths"))
            )
        )
        if not allowed_return_paths or not any(
            self._path_is_allowed(default_return_to, allowed)
            for allowed in allowed_return_paths
        ):
            raise ValueError(
                f"{prefix.upper()}_AUTH_DEFAULT_RETURN_TO must match "
                f"{prefix.upper()}_AUTH_ALLOWED_RETURN_PATHS"
            )
        return BrowserSurfaceProfile(
            surface=surface,
            client_id=str(getattr(self, f"{prefix}_casdoor_client_id")),
            client_secret=str(getattr(self, f"{prefix}_casdoor_client_secret")),
            redirect_uri=str(getattr(self, f"{prefix}_casdoor_redirect_uri")),
            application=str(getattr(self, f"{prefix}_casdoor_application")),
            frontend_base_url=frontend_base,
            frontend_origins=origins,
            policy_version=str(getattr(self, f"{prefix}_auth_policy_version")),
            role_allowlist=frozenset(
                self._split_csv(str(getattr(self, f"{prefix}_auth_role_allowlist")))
            ),
            scope_allowlist=frozenset(
                self._split_csv(str(getattr(self, f"{prefix}_auth_scope_allowlist")))
            ),
            default_return_to=default_return_to,
            allowed_return_paths=allowed_return_paths,
            required_scopes=(f"{self.app_slug}:admin",) if surface == "admin" else (),
            session_cookie_name=f"sunmoonai_{self.app_slug}_{surface}_sid",
            transaction_cookie_name=f"sunmoonai_{self.app_slug}_{surface}_oidc_tx",
            session_key_prefix=f"{self.app_slug}:auth:{surface}:session:",
            transaction_key_prefix=f"{self.app_slug}:auth:{surface}:oidc:",
        )

    @property
    def frontend_origin_list(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.browser_profile("admin").frontend_origins,
                    *self.browser_profile("web").frontend_origins,
                )
            )
        )

    def allowed_organizations(self, surface: BrowserSurface) -> frozenset[str]:
        if surface == "admin":
            return frozenset(self._split_csv(self.casdoor_organization))
        return frozenset(self._split_csv(self.web_casdoor_organizations))

    def require_browser_identity(self, surface: BrowserSurface | None = None) -> None:
        surfaces: tuple[BrowserSurface, ...] = (
            (surface,) if surface is not None else ("admin", "web")
        )
        for item in surfaces:
            organizations = self.allowed_organizations(item)
            if not organizations or any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", name)
                for name in organizations
            ):
                field = (
                    "CASDOOR_ORGANIZATION"
                    if item == "admin"
                    else "WEB_CASDOOR_ORGANIZATIONS"
                )
                raise ValueError(f"{field} must name at least one valid organization")
            if item == "admin" and len(organizations) != 1:
                raise ValueError(
                    "CASDOOR_ORGANIZATION must name exactly one organization"
                )
        provider_origin = self._strict_origin(
            self.casdoor_endpoint, field="CASDOOR_ENDPOINT"
        )
        discovery = urlsplit(self.casdoor_discovery_endpoint)
        if (
            discovery.scheme not in {"http", "https"}
            or not discovery.hostname
            or discovery.username
            or discovery.password
            or discovery.query
            or discovery.fragment
            or discovery.path != "/.well-known/openid-configuration"
        ):
            raise ValueError(
                "CASDOOR_DISCOVERY_URL must use the standard discovery path"
            )
        discovery_origin = f"{discovery.scheme}://{discovery.hostname}" + (
            f":{discovery.port}" if discovery.port is not None else ""
        )
        if discovery_origin != provider_origin:
            raise ValueError(
                "CASDOOR_DISCOVERY_URL must use the CASDOOR_ENDPOINT origin"
            )
        if self.is_production and urlsplit(provider_origin).scheme != "https":
            raise ValueError("CASDOOR_ENDPOINT must use HTTPS in production")

        for item in surfaces:
            profile = self.browser_profile(item)
            required = {
                f"{item.upper()}_CASDOOR_CLIENT_ID": profile.client_id,
                f"{item.upper()}_CASDOOR_CLIENT_SECRET": profile.client_secret,
                f"{item.upper()}_CASDOOR_REDIRECT_URI": profile.redirect_uri,
                f"{item.upper()}_FRONTEND_BASE_URL": profile.frontend_base_url,
            }
            missing = sorted(
                name for name, value in required.items() if not value.strip()
            )
            if missing:
                raise ValueError(
                    f"browser identity configuration missing: {', '.join(missing)}"
                )
            redirect = urlsplit(profile.redirect_uri)
            expected_path = f"/api/auth/{item}/callback"
            if (
                redirect.scheme not in {"http", "https"}
                or not redirect.hostname
                or redirect.username
                or redirect.password
                or redirect.query
                or redirect.fragment
                or redirect.path != expected_path
            ):
                raise ValueError(
                    f"{item.upper()}_CASDOOR_REDIRECT_URI must end at {expected_path}"
                )
            redirect_origin = f"{redirect.scheme}://{redirect.hostname}" + (
                f":{redirect.port}" if redirect.port is not None else ""
            )
            if redirect_origin != profile.frontend_base_url:
                raise ValueError(
                    f"{item.upper()}_CASDOOR_REDIRECT_URI must use the matching "
                    "FRONTEND_BASE_URL origin"
                )
            if profile.frontend_base_url not in profile.frontend_origins:
                raise ValueError(
                    f"{item.upper()}_FRONTEND_ALLOWED_ORIGINS must include "
                    "FRONTEND_BASE_URL"
                )
            if self.is_production and redirect.scheme != "https":
                raise ValueError(
                    f"{item.upper()}_CASDOOR_REDIRECT_URI must use HTTPS in production"
                )

    @property
    def downstream_allowed_path_prefix_list(self) -> tuple[str, ...]:
        return tuple(
            self._validate_relative_path(
                item, field="DOWNSTREAM_ALLOWED_PATH_PREFIXES"
            ).rstrip("/")
            for item in self._split_csv(self.downstream_allowed_path_prefixes)
        )

    def require_downstream_identity(self) -> None:
        required = {
            "DOWNSTREAM_BASE_URL": self.downstream_base_url,
            "DOWNSTREAM_CLIENT_ID": self.downstream_client_id,
            "DOWNSTREAM_CLIENT_SECRET": self.downstream_client_secret,
            "DOWNSTREAM_SCOPE": self.downstream_scope,
        }
        missing = sorted(name for name, value in required.items() if not value.strip())
        if missing:
            raise ValueError(
                f"downstream service configuration missing: {', '.join(missing)}"
            )
        downstream_origin = self._strict_origin(
            self.downstream_base_url, field="DOWNSTREAM_BASE_URL"
        )
        local_origins = {
            self.browser_profile("admin").frontend_base_url,
            self.browser_profile("web").frontend_base_url,
        }
        if downstream_origin in local_origins:
            raise ValueError("DOWNSTREAM_BASE_URL cannot target this Backend")
        if not self.downstream_allowed_path_prefix_list:
            raise ValueError("DOWNSTREAM_ALLOWED_PATH_PREFIXES cannot be empty")
        if self.is_production and not self.downstream_verify_ssl:
            raise ValueError("DOWNSTREAM_VERIFY_SSL must be true in production")

    @property
    def service_auth_subject_bindings(self) -> dict[str, frozenset[str]]:
        try:
            raw = json.loads(self.service_auth_subject_bindings_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "SERVICE_AUTH_SUBJECT_BINDINGS_JSON must be valid JSON"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON must be a JSON object")
        result: dict[str, frozenset[str]] = {}
        for subject, scopes in raw.items():
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or len(subject) > 512
                or not isinstance(scopes, list)
            ):
                raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON is invalid")
            normalized = frozenset(
                item.strip()
                for item in scopes
                if isinstance(item, str) and item.strip()
            )
            if len(normalized) != len(scopes) or any(
                len(item) > 128 for item in normalized
            ):
                raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON is invalid")
            result[subject.strip()] = normalized
        return result

    def require_service_identity(self) -> None:
        if not self.service_auth_audience.strip():
            raise ValueError("SERVICE_AUTH_AUDIENCE is required")
        if not self.service_auth_subject_bindings:
            raise ValueError("SERVICE_AUTH_SUBJECT_BINDINGS_JSON cannot be empty")
        if not self.casdoor_discovery_endpoint:
            raise ValueError("CASDOOR discovery is required for service identity")

    @property
    def celery_enabled(self) -> bool:
        return bool(self.celery_broker_url)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
