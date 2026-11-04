import pytest

from app.config import Settings, parse_initial_admin_work_nos


def test_initial_admin_work_numbers_are_explicit_and_deduplicated() -> None:
    assert parse_initial_admin_work_nos("") == ()
    assert parse_initial_admin_work_nos("10001, 10002,10001") == ("10001", "10002")
    with pytest.raises(ValueError, match="ADMIN_INITIAL_WORK_NOS"):
        parse_initial_admin_work_nos("REPLACE_WITH_ADMIN_WORK_NO")
from app.hermes_runtime.constants import (
    HERMES_0191_CONFIG_SCHEMA,
    HERMES_0191_STATE_SCHEMA,
    HERMES_0201_CONFIG_SCHEMA,
    HERMES_0201_STATE_SCHEMA,
    HERMES_PLATFORM_CONFIG_SCHEMA,
    HERMES_PLATFORM_STATE_SCHEMA,
    HERMES_DASHBOARD_PORT,
    HERMES_ENTERPRISE_KNOWLEDGE_MOUNT,
    HERMES_HOME,
    HERMES_WORKSPACE,
    append_url_path,
)


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_settings_use_final_runtime_contract() -> None:
    settings = _settings()

    assert settings.RUNTIME_CHAT_MODE == "contract"
    assert settings.HERMES_AUTH_MODE == "dev"
    assert settings.PORT_BACKEND == 8011
    assert settings.HERMES_PUBLIC_ASSET_ROOT == "/data/hermes/public-assets"
    assert settings.SKILLHUB_MAX_PACKAGE_BYTES == 25 * 1024 * 1024
    assert settings.SKILLHUB_MAX_FILE_COUNT == 500
    assert settings.SKILLHUB_MAX_SINGLE_FILE_BYTES == 25 * 1024 * 1024
    assert settings.APP_CENTER_MAX_PACKAGE_BYTES == 100 * 1024 * 1024
    assert settings.APP_CENTER_LAUNCH_TICKET_TTL_SECONDS == 60
    assert "HERMES_RUNTIME_IMAGE" not in Settings.model_fields
    assert settings.HERMES_RUNTIME_IDLE_TTL_SECONDS == 600
    assert "HERMES_APPLICATION_AGENT_RUNTIME_IMAGE" not in Settings.model_fields
    assert settings.HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS == 3900
    assert settings.HERMES_TUI_WS_ORPHAN_REAP_GRACE_S == 900
    assert settings.APPLICATION_AGENT_MAX_INFLIGHT_RUNS == 500
    assert settings.APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS == 600
    assert settings.APPLICATION_AGENT_HOT_SESSION_LIMIT == 64
    assert settings.APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS == 900
    assert "HERMES_UPGRADE_MIN_BUILD_FREE_BYTES" not in Settings.model_fields
    assert "HERMES_LOCAL_IMAGE_PUBLISHER" not in Settings.model_fields
    assert "COPAW" not in " ".join(Settings.model_fields)
    assert settings.PROVIDER_ROUTE_ISSUANCE_MODE == "off"
    assert settings.RUNTIME_PROVIDER_ROUTE_ACCEPTANCE_MODE == "compatible"
    assert settings.MCP_RUNTIME_IDENTITY_MODE == "asset_id"
    assert settings.mcp_runtime_identity_enabled(42)


def test_provider_route_canary_matches_only_exact_runtime_ids() -> None:
    settings = _settings(
        PROVIDER_ROUTE_ISSUANCE_MODE="canary",
        PROVIDER_ROUTE_CANARY_RUNTIME_IDS="runtime-10002, application-agent-25",
    )

    assert settings.provider_route_issuance_enabled("runtime-10002")
    assert settings.provider_route_issuance_enabled("application-agent-25")
    assert not settings.provider_route_issuance_enabled("runtime-4365")


def test_mcp_runtime_identity_canary_matches_only_stable_user_ids() -> None:
    settings = _settings(
        MCP_RUNTIME_IDENTITY_MODE="canary",
        MCP_RUNTIME_IDENTITY_CANARY_USER_IDS="42, 108",
    )

    assert settings.mcp_runtime_identity_enabled(42)
    assert settings.mcp_runtime_identity_enabled(108)
    assert not settings.mcp_runtime_identity_enabled(4)
    assert not settings.mcp_runtime_identity_enabled(109)


def test_application_runtime_warm_grace_cannot_exceed_ttl() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        _settings(
            APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS=901,
            APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        )


def test_application_agent_insecure_redirect_allowlist_is_exact_http_only() -> None:
    callback = (
        "http://192.0.2.80:8000/api/v1/hermes/browser-callback"
    )
    configured = _settings(
        APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST=[callback, callback]
    )
    assert configured.APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST == [
        callback
    ]

    for invalid in (
        "https://sqlbot.example.com/callback",
        "http://*.example.com/callback",
        "http://user:password@sqlbot.example.com/callback",
        "http://sqlbot.example.com/callback#fragment",
    ):
        with pytest.raises(ValueError, match="INSECURE_REDIRECT_URI_ALLOWLIST"):
            _settings(
                APPLICATION_AGENT_INSECURE_REDIRECT_URI_ALLOWLIST=[invalid]
            )


def test_runtime_image_keys_are_not_cached_in_settings() -> None:
    settings = _settings(
        HERMES_RUNTIME_IMAGE="personal-runtime:v10",
        HERMES_APPLICATION_AGENT_RUNTIME_IMAGE="application-runtime:v11",
    )

    assert not hasattr(settings, "HERMES_RUNTIME_IMAGE")
    assert not hasattr(settings, "HERMES_APPLICATION_AGENT_RUNTIME_IMAGE")


def test_settings_allow_explicit_real_runtime_mode() -> None:
    settings = _settings(
        RUNTIME_CHAT_MODE="real",
        RUNTIME_WEB_READY_RECEIPT_MODE="required",
        HERMES_AUTH_MODE="jwt",
        KUBECONFIG="/root/.kube/config",
    )

    assert settings.RUNTIME_CHAT_MODE == "real"
    assert settings.RUNTIME_WEB_READY_RECEIPT_MODE == "required"
    assert settings.HERMES_AUTH_MODE == "jwt"
    assert settings.KUBECONFIG == "/root/.kube/config"


def test_settings_reject_legacy_web_ready_receipt_modes() -> None:
    for mode in ("disabled", "optional"):
        with pytest.raises(ValueError):
            _settings(RUNTIME_WEB_READY_RECEIPT_MODE=mode)


def test_settings_allow_enterprise_idp_auth_mode_and_assertion_ttls() -> None:
    settings = _settings(
        HERMES_AUTH_MODE="enterprise_idpAES",
        ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS=21600,
        ENTERPRISE_IDP_CLOCK_SKEW_SECONDS=120,
    )

    assert settings.HERMES_AUTH_MODE == "enterprise_idpAES"
    assert settings.ENTERPRISE_IDP_ASSERTION_MAX_AGE_SECONDS == 21600
    assert settings.ENTERPRISE_IDP_CLOCK_SKEW_SECONDS == 120


def test_production_settings_require_enterprise_idp_aes_configuration() -> None:
    with pytest.raises(ValueError, match="ENTERPRISE_IDP_AES_KEY"):
        _settings(APP_ENV="production", HERMES_AUTH_MODE="enterprise_idpAES")

    configured = _settings(
        APP_ENV="production",
        HERMES_AUTH_MODE="enterprise_idpAES",
        ENTERPRISE_IDP_AES_KEY="0123456789abcdef",
        ENTERPRISE_IDP_AES_IV="fedcba9876543210",
        JWT_SECRET_KEY="production-secret",
        AES_ENCRYPTION_KEY="production-managed-aes-key",
        FILE_DOWNLOAD_SIGNING_SECRET="managed-openapi-file-signing-secret",
        RUNTIME_CHAT_MODE="real",
    )

    assert configured.HERMES_AUTH_MODE == "enterprise_idpAES"


def test_production_password_admin_mode_requires_a_managed_initial_password() -> None:
    production = {
        "APP_ENV": "production",
        "HERMES_AUTH_MODE": "enterprise_idpAES",
        "HERMES_ADMIN_AUTH_MODE": "password",
        "ENTERPRISE_IDP_AES_KEY": "0123456789abcdef",
        "ENTERPRISE_IDP_AES_IV": "fedcba9876543210",
        "JWT_SECRET_KEY": "production-secret",
        "AES_ENCRYPTION_KEY": "production-managed-aes-key",
        "FILE_DOWNLOAD_SIGNING_SECRET": "managed-openapi-file-signing-secret",
        "RUNTIME_CHAT_MODE": "real",
    }

    for password in ("", "short", "change-me-password", "<password>"):
        with pytest.raises(ValueError, match="ADMIN_INITIAL_PASSWORD"):
            _settings(**production, ADMIN_INITIAL_PASSWORD=password)

    configured = _settings(
        **production,
        ADMIN_INITIAL_PASSWORD="managed-admin-password",
    )

    assert configured.HERMES_ADMIN_AUTH_MODE == "password"


def test_production_settings_reject_default_jwt_secret() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _settings(
            APP_ENV="production",
            HERMES_AUTH_MODE="enterprise_idpAES",
            ENTERPRISE_IDP_AES_KEY="0123456789abcdef",
            ENTERPRISE_IDP_AES_IV="fedcba9876543210",
            JWT_SECRET_KEY="change-me-random-32-byte-hex",
            RUNTIME_CHAT_MODE="real",
        )


def test_production_settings_require_managed_general_aes_key() -> None:
    with pytest.raises(ValueError, match="AES_ENCRYPTION_KEY"):
        _settings(
            APP_ENV="production",
            HERMES_AUTH_MODE="enterprise_idpAES",
            ENTERPRISE_IDP_AES_KEY="0123456789abcdef",
            ENTERPRISE_IDP_AES_IV="fedcba9876543210",
            JWT_SECRET_KEY="production-secret",
            RUNTIME_CHAT_MODE="real",
        )


def test_platform_schema_contract_is_fixed_in_code() -> None:
    assert HERMES_DASHBOARD_PORT == 9119
    assert HERMES_HOME == "/opt/data"
    assert HERMES_WORKSPACE == "/data/workspace"
    assert HERMES_ENTERPRISE_KNOWLEDGE_MOUNT == "/knowledge/enterprise"
    assert HERMES_0191_STATE_SCHEMA == 23
    assert HERMES_0191_CONFIG_SCHEMA == 33
    assert HERMES_0201_STATE_SCHEMA == 25
    assert HERMES_0201_CONFIG_SCHEMA == 34
    assert HERMES_PLATFORM_STATE_SCHEMA == HERMES_0201_STATE_SCHEMA
    assert HERMES_PLATFORM_CONFIG_SCHEMA == HERMES_0201_CONFIG_SCHEMA


def test_application_runtime_prefers_single_pod_pvc_access() -> None:
    configured = _settings()

    assert (
        configured.HERMES_APPLICATION_AGENT_PVC_ACCESS_MODE
        == "ReadWriteOncePod"
    )
    assert (
        _settings(
            HERMES_APPLICATION_AGENT_PVC_ACCESS_MODE="ReadWriteOnce"
        ).HERMES_APPLICATION_AGENT_PVC_ACCESS_MODE
        == "ReadWriteOnce"
    )
    assert configured.APPLICATION_AGENT_CPU_REQUEST == "1m"
    assert configured.APPLICATION_AGENT_MEMORY_REQUEST == "200Mi"
    assert configured.PERSONAL_RUNTIME_CPU_REQUEST == "1m"
    assert configured.PERSONAL_RUNTIME_MEMORY_REQUEST == "200Mi"
    sized = _settings(
        APPLICATION_AGENT_CPU_REQUEST="500m",
        APPLICATION_AGENT_MEMORY_REQUEST="2Gi",
    )
    assert sized.APPLICATION_AGENT_CPU_REQUEST == "500m"
    assert sized.APPLICATION_AGENT_MEMORY_REQUEST == "2Gi"


def test_runtime_urls_are_derived_from_public_api_base() -> None:
    configured = _settings(PUBLIC_API_BASE="https://backend.example/root/")

    assert configured.platform_api_base_url == "https://backend.example/root/api/v1"
    assert configured.runtime_internal_base_url == "https://backend.example/root"
    assert not hasattr(configured, "model_relay_base_url")
    assert not hasattr(configured, "projection_claim_url")
    assert not hasattr(configured, "projection_receipt_url")
    assert append_url_path("https://backend.example/root/", "/api/v1") == (
        "https://backend.example/root/api/v1"
    )


def test_obsolete_runtime_settings_are_not_model_fields() -> None:
    removed = {
        "HERMES_RUNTIME_PROVIDER",
        "HERMES_PROJECTION_WORKER_IMAGE",
        "HERMES_RUNTIME_CONTAINER_PORT",
        "HERMES_RUNTIME_ENDPOINT_MODE",
        "HERMES_RUNTIME_WARMUP_MODE",
        "HERMES_RUNTIME_CHAT_TRANSPORT",
        "HERMES_RUNTIME_CONFIG_SOURCE",
        "HERMES_API_SERVER_HOST",
        "HERMES_HOME_MOUNT_PATH",
        "HERMES_WORKSPACE_MOUNT_PATH",
        "HERMES_RUNTIME_STATE_SCHEMA",
        "HERMES_RUNTIME_CONFIG_SCHEMA",
        "HERMES_RUNTIME_IMAGE_COMMIT",
        "HERMES_PLATFORM_API_BASE_URL",
        "MODEL_PROXY_BASE_URL",
        "PROJECTION_WORK_ITEM_ENDPOINT",
        "PROJECTION_WORK_ITEM_RECEIPT_ENDPOINT",
        "PROJECTION_WORK_ITEM_SECRET_CLEANUP",
        "ENTERPRISE_KNOWLEDGE_PV_NAME",
        "ENTERPRISE_KNOWLEDGE_PVC_NAME",
        "ENTERPRISE_KNOWLEDGE_STORAGE_SIZE",
        "ENTERPRISE_KNOWLEDGE_MOUNT_PATH",
    }

    assert removed.isdisjoint(Settings.model_fields)


def test_settings_enable_runtime_storage_maintenance() -> None:
    settings = _settings(HERMES_RUNTIME_STORAGE_MAINTENANCE=True)

    assert settings.HERMES_RUNTIME_STORAGE_MAINTENANCE is True


def test_settings_allow_runtime_session_agent_grace_override() -> None:
    settings = _settings(HERMES_TUI_WS_ORPHAN_REAP_GRACE_S=120)

    assert settings.HERMES_TUI_WS_ORPHAN_REAP_GRACE_S == 120


def test_office_preview_uses_separate_production_secret_and_local_fallback() -> None:
    local = _settings(RUNTIME_TOKEN_SECRET="local-runtime-secret")
    assert local.office_preview_jwt_secret == "local-runtime-secret"

    configured = _settings(ONLYOFFICE_JWT_SECRET="office-secret")
    assert configured.office_preview_jwt_secret == "office-secret"


def test_settings_expose_runtime_run_reconciliation_windows() -> None:
    settings = _settings(RUNTIME_RUN_RECONCILE_AFTER_SECONDS=120, RUNTIME_RUN_MAX_SECONDS=1800)

    assert settings.RUNTIME_RUN_RECONCILE_AFTER_SECONDS == 120
    assert settings.RUNTIME_RUN_MAX_SECONDS == 1800


def test_database_url_uses_async_mysql_driver() -> None:
    settings = _settings(
        MYSQL_USER="user",
        MYSQL_PASSWORD="password",
        MYSQL_HOST="mysql",
        MYSQL_PORT=3307,
        MYSQL_DB="hermes_test",
    )

    assert settings.database_url == (
        "mysql+asyncmy://user:password@mysql:3307/hermes_test?charset=utf8mb4"
    )
