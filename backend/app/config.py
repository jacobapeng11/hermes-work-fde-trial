from __future__ import annotations

from functools import cached_property
from ipaddress import ip_address
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.hermes_runtime.constants import append_url_path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    APP_ENV: str = "dev"
    PORT_BACKEND: int = 8011
    PUBLIC_WEB_BASE: str = "http://127.0.0.1:5173"
    PUBLIC_ADMIN_BASE: str = "http://127.0.0.1:5174"
    # Runtime Pods use the host's Nginx compatibility listener.  The Backend
    # itself remains loopback-only; see deploy/nginx/hermes-enterprise_idp-*.conf.
    PUBLIC_API_BASE: str = "http://127.0.0.1:8011"
    DESKTOP_RELEASE_DIR: Path = REPOSITORY_ROOT / "desktop_release"
    CORS_ORIGINS: str = (
        "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174"
    )

    JWT_SECRET_KEY: str = Field(default="change-me-random-32-byte-hex")
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 360
    PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(
        default=15,
        ge=5,
        le=60,
    )
    PLATFORM_SESSION_IDLE_TTL_SECONDS: int = Field(
        default=180,
        ge=60,
        le=3600,
    )
    APP_CENTER_LAUNCH_TICKET_TTL_SECONDS: int = Field(
        default=60,
        ge=10,
        le=300,
    )
    BROWSER_PRESENCE_HEARTBEAT_SECONDS: float = Field(
        default=15.0,
        ge=5.0,
        le=60.0,
    )
    BROWSER_PRESENCE_RUNTIME_TOUCH_SECONDS: float = Field(
        default=60.0,
        ge=15.0,
        le=300.0,
    )
    BROWSER_PRESENCE_FINAL_TOUCH_TIMEOUT_SECONDS: float = Field(
        default=5.0,
        ge=0.5,
        le=30.0,
    )
    ADMIN_PRESENCE_ONLINE_WINDOW_SECONDS: int = Field(
        default=120,
        ge=15,
        le=180,
    )
    ADMIN_PRESENCE_CACHE_TTL_SECONDS: int = Field(
        default=5,
        ge=0,
        le=60,
    )
    HERMES_AUTH_MODE: str = "dev"
    HERMES_ADMIN_AUTH_MODE: str = "inherit"
    # Contract-mode development may use the negative in-memory principal.
    # Any dev-auth flow backed by the real database must explicitly configure
    # an existing positive user id; the platform never seeds one implicitly.
    DEV_PRINCIPAL_USER_ID: int = -1
    # Used only to create the fixed `admin` account during the first password
    # mode database bootstrap. The plaintext is never persisted.
    ADMIN_INITIAL_PASSWORD: str = ""
    # Customer-provided comma-separated work numbers granted admin access at first seed.
    ADMIN_INITIAL_WORK_NOS: str = ""
    AES_ENCRYPTION_KEY: str = Field(default="change-me-random-16-byte-hex")
    RUNTIME_TOKEN_SECRET: str = Field(default="change-me-random-32-byte-hex")
    APPLICATION_AGENT_AUTHORIZATION_CODE_TTL_SECONDS: int = Field(
        default=120,
        ge=30,
        le=600,
    )
    APPLICATION_AGENT_TOKEN_TTL_SECONDS: int = Field(
        default=600,
        ge=60,
        le=3600,
    )
    APPLICATION_AGENT_SQLBOT2_TRUSTED_TOKEN_TTL_SECONDS: int = Field(
        default=3600,
        ge=60,
        le=3600,
    )
    APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST: list[str] = Field(
        default_factory=list,
        max_length=20,
    )
    APPLICATION_AGENT_WECOM_BINDING_TICKET_TTL_SECONDS: int = Field(
        default=300,
        ge=60,
        le=1800,
    )
    APPLICATION_AGENT_WECOM_OAUTH_STATE_TTL_SECONDS: int = Field(
        default=300,
        ge=60,
        le=900,
    )
    APPLICATION_AGENT_WECOM_CALLBACK_MAX_SKEW_SECONDS: int = Field(
        default=300,
        ge=30,
        le=1800,
    )
    APPLICATION_AGENT_MAX_INFLIGHT_RUNS: int = Field(
        default=500,
        ge=1,
        le=10_000,
    )
    APPLICATION_AGENT_CPU_REQUEST: str = Field(
        default="1m",
        min_length=1,
        max_length=32,
    )
    APPLICATION_AGENT_MEMORY_REQUEST: str = Field(
        default="200Mi",
        min_length=1,
        max_length=32,
    )
    APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS: int = Field(
        default=600,
        ge=1,
        le=3600,
    )
    APPLICATION_AGENT_HOT_SESSION_LIMIT: int = Field(
        default=64,
        ge=0,
        le=10_000,
    )
    APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS: int = Field(
        default=900,
        ge=1,
        le=7200,
    )
    HERMES_APPLICATION_AGENT_PVC_ACCESS_MODE: Literal[
        "ReadWriteOncePod",
        "ReadWriteOnce",
    ] = "ReadWriteOncePod"
    APPLICATION_AGENT_WECOM_EVENT_SCAN_INTERVAL_SECONDS: float = Field(
        default=2.0,
        ge=0.5,
        le=60.0,
    )
    WECOM_API_BASE_URL: str = "https://qyapi.weixin.qq.com"
    WECOM_OAUTH_AUTHORIZE_URL: str = "https://open.weixin.qq.com/connect/oauth2/authorize"
    IM_GATEWAY_ENABLED: bool = False
    IM_GATEWAY_NODE_BINARY: str = "node"
    IM_GATEWAY_RECONCILE_INTERVAL_SECONDS: float = Field(default=5.0, ge=0.5, le=60.0)
    IM_GATEWAY_HEARTBEAT_INTERVAL_SECONDS: float = Field(default=10.0, ge=1.0, le=60.0)
    IM_GATEWAY_LEASE_TTL_SECONDS: int = Field(default=30, ge=5, le=300)
    IM_GATEWAY_CONNECTION_CAPACITY: int = Field(default=100, ge=1, le=10_000)
    IM_GATEWAY_QUEUE_POLL_SECONDS: float = Field(default=0.5, ge=0.1, le=30.0)
    IM_GATEWAY_MAX_MESSAGE_ATTEMPTS: int = Field(default=3, ge=1, le=20)
    IM_GATEWAY_REASONING_FLUSH_SECONDS: float = Field(default=0.8, ge=0.1, le=5.0)
    IM_GATEWAY_ONBOARDING_POLL_SECONDS: float = Field(default=3.0, ge=1.0, le=30.0)
    IM_GATEWAY_ONBOARDING_TTL_SECONDS: int = Field(default=300, ge=60, le=900)
    IM_GATEWAY_MEDIA_MAX_MB: int = Field(default=100, ge=1, le=100)
    WECOM_QR_GENERATE_URL: str = "https://work.weixin.qq.com/ai/qc/generate"
    WECOM_QR_QUERY_URL: str = "https://work.weixin.qq.com/ai/qc/query_result"
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = "change-me"
    MYSQL_DB: str = "hermes_cloud"
    MYSQL_SSL_MODE: str = "DISABLED"
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    DEFAULT_ORG_ID: int = 1
    # Map directory company IDs to display names for the customer's HR system.
    COMPANY_DIRECTORY_NAMES: dict[str, str] = Field(
        default_factory=lambda: {"1": "示例公司", "2": "示例业务公司", "3": "示例子公司"}
    )
    # enterprise_idpAES: custom AES assertions issued by the login_mobile service.
    ENTERPRISE_IDP_AES_KEY: str = ""
    ENTERPRISE_IDP_AES_IV: str = ""
    ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS: int = 6 * 60 * 60
    ENTERPRISE_IDP_CLOCK_SKEW_SECONDS: int = 120
    ENTERPRISE_IDP_TIMEZONE: str = "Asia/Shanghai"

    # Standard OAuth2; the current enterprise provider is enterprise_idp OAuth2.
    OAUTH2_PROVIDER_ID: str = ""
    OAUTH2_CLIENT_ID: str = ""
    OAUTH2_CLIENT_SECRET: str = Field(default="", repr=False)
    OAUTH2_AUTHORIZATION_URL: str = ""
    OAUTH2_TOKEN_URL: str = ""
    OAUTH2_USERINFO_URL: str = ""
    OAUTH2_REDIRECT_URI: str = ""
    OAUTH2_SCOPES: str = ""
    OAUTH2_PKCE_ENABLED: bool = True
    OAUTH2_ALLOW_INSECURE_HTTP: bool = False
    OAUTH2_TOKEN_AUTH_METHOD: str = "client_secret_basic"
    OAUTH2_USERINFO_METHOD: str = "GET"
    OAUTH2_USER_ID_FIELD: str = ""
    OAUTH2_USER_WORK_NO_FIELD: str = ""
    OAUTH2_USER_NAME_FIELD: str = ""
    OAUTH2_USER_EMAIL_FIELD: str = ""
    OAUTH2_AUTHORIZATION_PARAMS: str = "{}"
    OAUTH2_LOGIN_TRANSACTION_TTL_SECONDS: int = Field(default=300, ge=60, le=600)
    OAUTH2_HTTP_TIMEOUT_SECONDS: float = Field(default=10.0, ge=1.0, le=30.0)
    OAUTH2_CONFIG_REVISION: str = Field(default="1", pattern=r"^[1-9][0-9]*$")


    HERMES_RUNTIME_NAMESPACE: str = "hermes-runtimes"
    # Runtime image selection is read directly from backend/.env by
    # RuntimeImageConfigStore so long-lived processes observe atomic upgrades
    # without caching the active image in Settings.
    HERMES_PLATFORM_REVISION: str = "workspace"
    HERMES_RUNTIME_SERVICE_ACCOUNT: str = "hermes-runtime-workload"
    HERMES_RUNTIME_INGRESS_BASE_URL: str = "http://127.0.0.1:31080"
    RUNTIME_EDGE_MAX_PROMPT_BYTES: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=16 * 1024 * 1024,
    )
    RUNTIME_EDGE_MAX_OUTPUT_TOKENS: int = Field(
        default=32768,
        ge=1,
        le=1_000_000,
    )
    PROVIDER_ROUTE_ISSUANCE_MODE: Literal["off", "canary", "on"] = "off"
    PROVIDER_ROUTE_CANARY_RUNTIME_IDS: str = ""
    RUNTIME_PROVIDER_ROUTE_ACCEPTANCE_MODE: Literal[
        "compatible",
        "required",
    ] = "compatible"

    def provider_route_issuance_enabled(self, runtime_id: str) -> bool:
        if self.PROVIDER_ROUTE_ISSUANCE_MODE == "on":
            return True
        if self.PROVIDER_ROUTE_ISSUANCE_MODE != "canary":
            return False
        return runtime_id in {
            value.strip()
            for value in self.PROVIDER_ROUTE_CANARY_RUNTIME_IDS.split(",")
            if value.strip()
        }

    MCP_RUNTIME_IDENTITY_MODE: Literal[
        "legacy_slug",
        "canary",
        "asset_id",
    ] = "asset_id"
    MCP_RUNTIME_IDENTITY_CANARY_USER_IDS: str = ""

    def mcp_runtime_identity_enabled(self, user_id: int) -> bool:
        if self.MCP_RUNTIME_IDENTITY_MODE == "asset_id":
            return True
        if self.MCP_RUNTIME_IDENTITY_MODE != "canary":
            return False
        return int(user_id) in {
            int(value.strip())
            for value in self.MCP_RUNTIME_IDENTITY_CANARY_USER_IDS.split(",")
            if value.strip().isdigit() and int(value.strip()) > 0
        }

    HERMES_RUNTIME_ROOT: str = "/data/hermes/runtimes"
    HERMES_APPLICATION_AGENT_RUNTIME_ROOT: str = "/data/hermes/application-agents"
    HERMES_PUBLIC_ASSET_ROOT: str = "/data/hermes/public-assets"
    # SkillHub is deliberately a server-side integration. Every package is
    # copied into the owner's Runtime volume; this is never a shared cache.
    SKILLHUB_ENABLED: bool = True
    SKILLHUB_API_BASE_URL: str = "https://api.skillhub.cn"
    SKILLHUB_PROXY_URL: str = ""
    SKILLHUB_PROXY_CONNECT_IP: str = ""
    SKILLHUB_ALLOWED_FEEDS: str = "featured,hot,trending"
    SKILLHUB_DEFAULT_PAGE_SIZE: int = Field(default=12, ge=1, le=20)
    SKILLHUB_MAX_PAGE_SIZE: int = Field(default=20, ge=1, le=50)
    SKILLHUB_MIN_DOWNLOADS: int = Field(default=1000, ge=0)
    SKILLHUB_MIN_INSTALLS: int = Field(default=100, ge=0)
    SKILLHUB_CATEGORY_MAPPING_JSON: str = (
        '{"knowledge-management":"research","software-development":"software-development",'
        '"data-analysis":"data-science","design":"creative","ai-agent":"agent-automation"}'
    )
    SKILLHUB_VERSION_CHECK_TTL_SECONDS: int = Field(default=21600, ge=60)
    SKILLHUB_REQUEST_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=60)
    SKILLHUB_MAX_PACKAGE_BYTES: int = Field(default=25 * 1024 * 1024, ge=1)
    SKILLHUB_MAX_UNPACKED_BYTES: int = Field(default=100 * 1024 * 1024, ge=1)
    SKILLHUB_MAX_FILE_COUNT: int = Field(default=500, ge=1)
    SKILLHUB_MAX_SINGLE_FILE_BYTES: int = Field(default=25 * 1024 * 1024, ge=1)
    SKILLHUB_SIGNATURE_REQUIRED: bool = True
    APP_CENTER_MAX_PACKAGE_BYTES: int = Field(default=100 * 1024 * 1024, ge=1)
    APP_CENTER_MAX_APPS_PER_USER: int = Field(default=100, ge=1, le=1000)
    # The only public MCP automatically installed for a newly created
    # personal Runtime. Deployments can override this with their managed
    # web-search MCP slug, or disable it with an empty value.
    PERSONAL_DEFAULT_MCP_SLUG: str = "web-search-mcp"
    PERSONAL_DEFAULT_MCP_RETRY_SECONDS: float = Field(default=15.0, ge=1.0, le=300.0)
    PROFILE_CONVERGENCE_POLL_SECONDS: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
    )
    PROFILE_CONVERGENCE_LEASE_SECONDS: int = Field(
        default=180,
        ge=30,
        le=900,
    )
    PROFILE_CONVERGENCE_CONCURRENCY: int = Field(
        default=4,
        ge=1,
        le=32,
    )
    PROFILE_CONVERGENCE_BATCH_SIZE: int = Field(
        default=16,
        ge=1,
        le=256,
    )
    MCP_REGISTRY_CONVERGENCE_POLL_SECONDS: float = Field(
        default=0.5,
        ge=0.1,
        le=60.0,
    )
    MCP_REGISTRY_CONVERGENCE_LEASE_SECONDS: int = Field(
        default=180,
        ge=30,
        le=900,
    )
    MCP_REGISTRY_CONVERGENCE_CONCURRENCY: int = Field(
        default=2,
        ge=1,
        le=16,
    )
    MCP_REGISTRY_CONVERGENCE_BATCH_SIZE: int = Field(
        default=8,
        ge=1,
        le=128,
    )
    HERMES_RUNTIME_NODE_NAME: str = "demo"
    PERSONAL_RUNTIME_CPU_REQUEST: str = Field(
        default="1m",
        min_length=1,
        max_length=32,
    )
    PERSONAL_RUNTIME_MEMORY_REQUEST: str = Field(
        default="200Mi",
        min_length=1,
        max_length=32,
    )
    HERMES_RUNTIME_STORAGE_MAINTENANCE: bool = False
    ENTERPRISE_KNOWLEDGE_HOST_PATH: str = ""
    HERMES_RUNTIME_IDLE_TTL_SECONDS: int = Field(default=600, ge=1)
    RUNTIME_WARM_KEEPALIVE_INTERVAL_SECONDS: int = Field(default=30, ge=10, le=600)
    RUNTIME_WARM_IDLE_TTL_SECONDS: int = Field(default=900, ge=60, le=86_400)
    RUNTIME_WARM_RECEIPT_FRESH_SECONDS: int = Field(default=180, ge=30, le=3600)
    HERMES_RUNTIME_READY_TIMEOUT_SECONDS: int = 120
    HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS: int = 3900
    # Legacy Web clarify expiry. New requests no longer terminate on this
    # value; it is retained for deployed environment compatibility.
    HERMES_USER_WEB_CLARIFY_TIMEOUT_SECONDS: int = Field(
        default=3600,
        ge=-1,
        le=86_400,
    )
    # Auto-default is a Backend-owned continuation trigger, deliberately
    # separate from the legacy terminal expiry field.  0 and -1 disable it.
    HERMES_USER_WEB_CLARIFY_AUTO_DEFAULT_SECONDS: int = Field(
        default=60,
        ge=-1,
        le=86_400,
    )
    HERMES_USER_WEB_CLARIFY_SCAN_INTERVAL_SECONDS: int = Field(
        default=5,
        ge=1,
        le=3_600,
    )
    HERMES_USER_WEB_CLARIFY_SCAN_BATCH_SIZE: int = Field(
        default=25,
        ge=1,
        le=100,
    )
    HERMES_USER_WEB_CLARIFY_CLAIM_LEASE_SECONDS: int = Field(
        default=120,
        ge=90,
        le=3_600,
    )
    HERMES_USER_WEB_CLARIFY_MAX_AUTO_RESUME_ATTEMPTS: int = Field(
        default=12,
        ge=1,
        le=1_000,
    )
    HERMES_DASHBOARD_HOT_CHANNEL_IDLE_GRACE_SECONDS: int = Field(default=90, ge=30, le=300)
    HERMES_TUI_WS_ORPHAN_REAP_GRACE_S: int = Field(default=900, ge=1)
    KUBECONFIG: str = ""
    # Dedicated least-privilege identity for the Admin resource console.
    # Never silently fall back to the Runtime provisioning kubeconfig.
    ADMIN_KUBECONFIG: str = ""
    NEWAPI_BASE_URL: str = "http://newapi:3000/v1"
    NEWAPI_API_KEY_SECRET_NAME: str = "hermes-newapi-runtime"

    RUNTIME_CHAT_MODE: str = "contract"
    # Web chat has one admission contract.  Keeping this as a literal makes
    # stale optional/disabled deployment configuration fail at startup
    # instead of silently re-enabling the legacy slow path.
    RUNTIME_WEB_READY_RECEIPT_MODE: Literal["required"] = "required"
    RUNTIME_PREPARED_KNOWLEDGE_TTL_SECONDS: int = Field(
        default=180,
        ge=90,
        le=3600,
    )
    RUNTIME_RUN_RECONCILE_AFTER_SECONDS: int = 300
    RUNTIME_RUN_MAX_SECONDS: int = 3600
    RUNTIME_RUN_CANCEL_GRACE_SECONDS: int = 30
    RUNTIME_RUN_RECONCILE_INTERVAL_SECONDS: int = 60
    RUNTIME_SCHEMA_STARTUP_RECONCILE_INTERVAL_SECONDS: int = 5
    RUNTIME_RUN_EVENT_RETENTION_SECONDS: int = 86400
    RUNTIME_KANBAN_LEASE_RECONCILE_INTERVAL_SECONDS: int = Field(default=30, ge=5, le=300)
    RUNTIME_KANBAN_TEMPORARY_LEASE_SECONDS: int = Field(default=120, ge=15, le=600)
    RUNTIME_KANBAN_LEASE_RELEASE_GRACE_SECONDS: int = Field(default=60, ge=0, le=900)
    EXPERT_COLLABORATION_RECONCILE_INTERVAL_SECONDS: float = Field(
        default=2.0,
        ge=0.5,
        le=30.0,
    )
    EXPERT_COLLABORATION_WAIT_BACKOFF_MAX_SECONDS: int = Field(
        default=60,
        ge=2,
        le=3600,
    )
    EXPERT_COLLABORATION_WAIT_MAX_ATTEMPTS: int = Field(
        default=100,
        ge=3,
        le=10_000,
    )
    EXPERT_COLLABORATION_LEASE_SECONDS: int = Field(default=300, ge=30, le=1800)
    EXPERT_COLLABORATION_MAX_SYNTHESIS_ATTEMPTS: int = Field(
        default=3,
        ge=1,
        le=10,
    )
    EXPERT_COLLABORATION_SSE_POLL_SECONDS: float = Field(
        default=1.0,
        ge=0.25,
        le=10.0,
    )
    EXPERT_RELEASE_ARTIFACT_RETENTION_DAYS: int = Field(default=30, ge=1)
    EXPERT_RELEASE_ARTIFACT_CLEANUP_INTERVAL_SECONDS: float = Field(
        default=3600.0,
        ge=60.0,
    )
    # Hermes starts its self-improvement review asynchronously after a user
    # turn has completed.  These bounds authorize only that short follow-up
    # activity without keeping the user-facing run active.
    POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS: int = Field(default=30, ge=1)
    POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS: int = Field(default=15, ge=1)
    POST_TURN_REVIEW_MAX_DURATION_SECONDS: int = Field(default=300, ge=1)
    SCHEDULED_TASK_SCAN_INTERVAL_SECONDS: float = Field(default=10.0, ge=0.1)
    SCHEDULED_TASK_SESSION_RETRY_INTERVAL_SECONDS: float = Field(default=2.0, ge=0.1)
    SCHEDULED_TASK_SESSION_ADMISSION_TIMEOUT_SECONDS: float = Field(default=60.0, ge=1.0)
    SCHEDULED_TASK_EXECUTION_LEASE_SECONDS: int = Field(default=90, ge=30, le=3600)
    SCHEDULED_TASK_EXECUTION_HEARTBEAT_SECONDS: float = Field(default=20.0, ge=1.0, le=300.0)
    SCHEDULED_TASK_SCAN_BATCH_SIZE: int = Field(default=100, ge=1, le=1000)
    CHAT_FILE_MAX_UPLOAD_MB: int = 100
    CHAT_FILE_ALLOWED_ROOT: str = "workspace"
    FILE_DOWNLOAD_SIGNING_SECRET: str = ""
    FILE_DOWNLOAD_URL_TTL_SECONDS: int = Field(
        default=86_400,
        ge=300,
        le=604_800,
    )
    ONLYOFFICE_PUBLIC_URL: str = ""
    ONLYOFFICE_INTERNAL_API_BASE: str = ""
    ONLYOFFICE_JWT_SECRET: str = ""
    ONLYOFFICE_PREVIEW_TOKEN_TTL_SECONDS: int = Field(default=300, ge=60, le=3600)

    KNOWLEDGE_ASSERTION_ISSUER: str = "hermes-work"
    KNOWLEDGE_ASSERTION_TTL_SECONDS: int = Field(default=60, ge=15, le=300)
    KNOWLEDGE_DATA_SERVICE_BASE_URL: str = ""
    KNOWLEDGE_DATA_SERVICE_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=120)
    KNOWLEDGE_SQL_QUERY_TIMEOUT_SECONDS: int = Field(default=70, ge=1, le=120)
    KNOWLEDGE_DATA_SERVICE_VERIFY_TLS: bool = True
    KNOWLEDGE_DATA_SERVICE_CA_FILE: str = ""
    DATA_SERVICE_ASSERTION_SECRET: str = ""
    DATA_SERVICE_ASSERTION_AUDIENCE: str = "enterprise-data"
    KNOWLEDGE_GOVERNANCE_SOURCE_ROOT: str = ""
    KNOWLEDGE_GOVERNANCE_COMPILE_TIMEOUT_SECONDS: int = Field(
        default=30,
        ge=5,
        le=120,
    )
    KNOWLEDGE_GOVERNANCE_MAX_SNAPSHOT_BYTES: int = Field(
        default=8 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024,
    )
    KNOWLEDGE_GOVERNANCE_MAX_NODES: int = Field(
        default=10_000,
        ge=1,
        le=100_000,
    )
    KNOWLEDGE_GOVERNANCE_MAX_EDGES: int = Field(
        default=20_000,
        ge=1,
        le=200_000,
    )
    KNOWLEDGE_DOCUMENT_SEARCH_URL: str = ""
    KNOWLEDGE_DOCUMENT_SERVICE_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=120)

    PERSONAL_KNOWLEDGE_PROVIDER: Literal["filebase", "weknora"] = "filebase"
    PERSONAL_KNOWLEDGE_SYNC_BASE_URL: str = ""
    PERSONAL_KNOWLEDGE_SEARCH_URL: str = ""
    PERSONAL_KNOWLEDGE_MIN_SIMILARITY: float = Field(default=0.0, ge=0, le=1)
    PERSONAL_KNOWLEDGE_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=600)
    WEKNORA_API_BASE_URL: str = ""
    WEKNORA_API_KEY: str = ""
    WEKNORA_EMBEDDING_MODEL_ID: str = ""
    WEKNORA_SUMMARY_MODEL_ID: str = ""
    WEKNORA_WIKI_SYNTHESIS_MODEL_ID: str = ""
    WEKNORA_WIKI_EXTRACTION_GRANULARITY: str = "exhaustive"
    WEKNORA_TIMEOUT_SECONDS: int = Field(default=30, ge=1, le=600)
    PERSONAL_KNOWLEDGE_OSS_UPLOAD_URL: str = ""
    PERSONAL_KNOWLEDGE_OSS_BUCKET: str = "hermes-portal"
    PERSONAL_KNOWLEDGE_OSS_PUBLIC_BASE_URL: str = ""
    PERSONAL_KNOWLEDGE_OSS_INTERNAL_HOST: str = ""
    PERSONAL_KNOWLEDGE_MAX_UPLOAD_MB: int = Field(default=100, ge=1, le=100)
    AVATAR_OSS_UPLOAD_URL: str = ""
    AVATAR_OSS_BUCKET: str = "hermes-portal"
    AVATAR_OSS_PUBLIC_BASE_URL: str = ""
    AVATAR_OSS_INTERNAL_HOST: str = ""
    AVATAR_MAX_UPLOAD_MB: int = Field(default=2, ge=1, le=10)
    AVATAR_MAX_SOURCE_PIXELS: int = Field(default=16_777_216, ge=65_536)

    @field_validator("RUNTIME_CHAT_MODE")
    @classmethod
    def validate_runtime_chat_mode(cls, value: str) -> str:
        allowed = {"contract", "real"}
        if value not in allowed:
            raise ValueError(f"RUNTIME_CHAT_MODE must be one of {sorted(allowed)}")
        return value

    @field_validator("HERMES_AUTH_MODE")
    @classmethod
    def validate_auth_mode(cls, value: str) -> str:
        allowed = {"dev", "jwt", "enterprise_idpAES", "oauth2"}
        if value not in allowed:
            raise ValueError(f"HERMES_AUTH_MODE must be one of {sorted(allowed)}")
        return value

    @field_validator("HERMES_ADMIN_AUTH_MODE")
    @classmethod
    def validate_admin_auth_mode(cls, value: str) -> str:
        allowed = {"inherit", "dev", "password"}
        if value not in allowed:
            raise ValueError(f"HERMES_ADMIN_AUTH_MODE must be one of {sorted(allowed)}")
        return value

    @field_validator("APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST")
    @classmethod
    def validate_application_agent_insecure_redirect_uri_allowlist(
        cls,
        values: list[str],
    ) -> list[str]:
        normalized: list[str] = []
        for raw_value in values:
            value = raw_value.strip()
            parsed = urlparse(value)
            if (
                not value
                or len(value) > 2_048
                or "*" in value
                or parsed.scheme != "http"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError(
                    "APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST must "
                    "contain exact absolute HTTP URLs without credentials, "
                    "wildcards, or fragments"
                )
            normalized.append(value)
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_production_auth_boundary(self) -> "Settings":
        self._validate_skillhub()
        if (
            self.APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS
            > self.APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS
        ):
            raise ValueError(
                "APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS cannot exceed "
                "APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS"
            )
        if self.HERMES_AUTH_MODE == "oauth2":
            from app.services.oauth2 import validate_oauth2_settings
            validate_oauth2_settings(self)
        if self.APP_ENV.lower() != "production":
            return self

        if self.HERMES_AUTH_MODE not in {"enterprise_idpAES", "oauth2"}:
            raise ValueError("production requires HERMES_AUTH_MODE=enterprise_idpAES or oauth2")
        if self.HERMES_ADMIN_AUTH_MODE == "password":
            _validate_admin_initial_password(self.ADMIN_INITIAL_PASSWORD)
        if self.HERMES_AUTH_MODE == "enterprise_idpAES":
            required_enterprise_idp = {
                "ENTERPRISE_IDP_AES_KEY": self.ENTERPRISE_IDP_AES_KEY,
                "ENTERPRISE_IDP_AES_IV": self.ENTERPRISE_IDP_AES_IV,
            }
            missing = [name for name, value in required_enterprise_idp.items() if not value.strip()]
            if missing:
                raise ValueError(f"production requires EnterpriseIdp settings: {', '.join(missing)}")
        if self.JWT_SECRET_KEY == "change-me-random-32-byte-hex":
            raise ValueError("production requires a managed JWT_SECRET_KEY")
        if self.AES_ENCRYPTION_KEY == "change-me-random-16-byte-hex":
            raise ValueError("production requires a managed AES_ENCRYPTION_KEY")
        if self.HERMES_AUTH_MODE == "enterprise_idpAES":
            if len(self.ENTERPRISE_IDP_AES_KEY.encode("utf-8")) != 16:
                raise ValueError("production ENTERPRISE_IDP_AES_KEY must be exactly 16 bytes")
            if len(self.ENTERPRISE_IDP_AES_IV.encode("utf-8")) != 16:
                raise ValueError("production ENTERPRISE_IDP_AES_IV must be exactly 16 bytes")
            if self.ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS != 6 * 60 * 60:
                raise ValueError("production EnterpriseIdp assertion lifetime must be six hours")
        if self.RUNTIME_CHAT_MODE != "real":
            raise ValueError("production requires real runtime chat mode")
        file_download_secret = self.FILE_DOWNLOAD_SIGNING_SECRET.strip()
        if len(file_download_secret) < 32:
            raise ValueError("production requires a managed FILE_DOWNLOAD_SIGNING_SECRET")
        if self.ONLYOFFICE_PUBLIC_URL and not self.ONLYOFFICE_JWT_SECRET:
            raise ValueError("production Office preview requires ONLYOFFICE_JWT_SECRET")
        return self

    def _validate_skillhub(self) -> None:
        if not self.SKILLHUB_ENABLED:
            return
        parsed = urlparse(self.SKILLHUB_API_BASE_URL)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(
                "SKILLHUB_API_BASE_URL must be an absolute HTTPS URL without credentials"
            )
        proxy_url = self.SKILLHUB_PROXY_URL.strip()
        proxy_connect_ip = self.SKILLHUB_PROXY_CONNECT_IP.strip()
        if bool(proxy_url) != bool(proxy_connect_ip):
            raise ValueError(
                "SKILLHUB_PROXY_URL and SKILLHUB_PROXY_CONNECT_IP must be configured together"
            )
        if proxy_url:
            proxy = urlparse(proxy_url)
            if (
                proxy.scheme != "socks5"
                or not proxy.hostname
                or proxy.path not in {"", "/"}
                or proxy.query
                or proxy.fragment
            ):
                raise ValueError("SKILLHUB_PROXY_URL must be an absolute SOCKS5 URL")
            if proxy.username or proxy.password:
                raise ValueError("SKILLHUB_PROXY_URL must not contain credentials")
            try:
                proxy_port = proxy.port
            except ValueError as exc:
                raise ValueError("SKILLHUB_PROXY_URL must contain a valid port") from exc
            if proxy_port is None:
                raise ValueError("SKILLHUB_PROXY_URL must contain a valid port")
            try:
                proxy_is_loopback = ip_address(proxy.hostname).is_loopback
            except ValueError:
                proxy_is_loopback = proxy.hostname.casefold() == "localhost"
            if not proxy_is_loopback:
                raise ValueError("SKILLHUB_PROXY_URL must target a loopback host")
            try:
                ip_address(proxy_connect_ip)
            except ValueError as exc:
                raise ValueError("SKILLHUB_PROXY_CONNECT_IP must be an IP address") from exc
        feeds = {item.strip() for item in self.SKILLHUB_ALLOWED_FEEDS.split(",") if item.strip()}
        if not feeds or not feeds.issubset({"featured", "hot", "trending"}):
            raise ValueError("SKILLHUB_ALLOWED_FEEDS must only contain featured, hot, trending")
        if self.SKILLHUB_DEFAULT_PAGE_SIZE > self.SKILLHUB_MAX_PAGE_SIZE:
            raise ValueError("SKILLHUB_DEFAULT_PAGE_SIZE cannot exceed SKILLHUB_MAX_PAGE_SIZE")
        if self.SKILLHUB_MAX_UNPACKED_BYTES < self.SKILLHUB_MAX_PACKAGE_BYTES:
            raise ValueError(
                "SKILLHUB_MAX_UNPACKED_BYTES cannot be lower than SKILLHUB_MAX_PACKAGE_BYTES"
            )
        if self.SKILLHUB_MAX_SINGLE_FILE_BYTES > self.SKILLHUB_MAX_UNPACKED_BYTES:
            raise ValueError(
                "SKILLHUB_MAX_SINGLE_FILE_BYTES cannot exceed SKILLHUB_MAX_UNPACKED_BYTES"
            )
        try:
            mapping = json.loads(self.SKILLHUB_CATEGORY_MAPPING_JSON)
        except json.JSONDecodeError as exc:
            raise ValueError("SKILLHUB_CATEGORY_MAPPING_JSON must be valid JSON") from exc
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
        ):
            raise ValueError("SKILLHUB_CATEGORY_MAPPING_JSON must be a string-to-string object")
        allowed_categories = {
            "hermes-core",
            "browser",
            "productivity",
            "software-development",
            "data-science",
            "research",
            "creative",
            "email",
            "github",
            "media",
            "mlops",
            "note-taking",
            "social-media",
            "autonomous-ai-agents",
            "agent-automation",
            "security",
            "devops",
            "finance",
            "communication",
            "web-development",
            "other",
        }
        unknown = sorted(set(mapping.values()) - allowed_categories)
        if unknown:
            raise ValueError(
                f"SKILLHUB_CATEGORY_MAPPING_JSON contains unknown category keys: {', '.join(unknown)}"
            )

    @cached_property
    def skillhub_category_mapping(self) -> dict[str, str]:
        return dict(json.loads(self.SKILLHUB_CATEGORY_MAPPING_JSON))

    @cached_property
    def skillhub_allowed_feeds(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.SKILLHUB_ALLOWED_FEEDS.split(",") if item.strip()
        )

    @cached_property
    def database_url(self) -> str:
        return (
            "mysql+asyncmy://"
            f"{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"
            "?charset=utf8mb4"
        )

    @cached_property
    def platform_api_base_url(self) -> str:
        return append_url_path(self.PUBLIC_API_BASE, "/api/v1")

    @cached_property
    def runtime_internal_base_url(self) -> str:
        return self.PUBLIC_API_BASE.rstrip("/")

    @cached_property
    def office_preview_jwt_secret(self) -> str:
        if self.ONLYOFFICE_JWT_SECRET:
            return self.ONLYOFFICE_JWT_SECRET
        return self.RUNTIME_TOKEN_SECRET if self.APP_ENV.lower() != "production" else ""

    @cached_property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


def parse_initial_admin_work_nos(raw: str) -> tuple[str, ...]:
    work_nos = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if any(value.startswith("REPLACE_") for value in work_nos):
        raise ValueError("ADMIN_INITIAL_WORK_NOS must contain customer work numbers")
    return work_nos


def _validate_admin_initial_password(value: str) -> None:
    """Reject missing, weak, and documentation-placeholder bootstrap passwords."""
    normalized = value.strip().lower()
    placeholders = {
        "",
        "admin",
        "password",
        "changeme",
        "change-me",
        "example",
        "example-password",
        "your-password",
        "<password>",
    }
    if (
        len(value.encode("utf-8")) < 12
        or normalized in placeholders
        or normalized.startswith("<")
        or normalized.startswith("example")
        or normalized.startswith("change-me")
    ):
        raise ValueError(
            "production password admin mode requires a managed ADMIN_INITIAL_PASSWORD of at least 12 bytes"
        )


settings = Settings()
