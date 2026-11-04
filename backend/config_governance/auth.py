from __future__ import annotations

from pydantic import Field


class AuthSettings:
    APP_ENV: str = "dev"
    PORT_BACKEND: int = 8011
    PUBLIC_WEB_BASE: str = "http://127.0.0.1:5173"
    PUBLIC_ADMIN_BASE: str = "http://127.0.0.1:5174"
    # Runtime Pods use the host's Nginx compatibility listener. The Backend
    # itself remains loopback-only; see deploy/nginx/hermes-enterprise_idp-*.conf.
    PUBLIC_API_BASE: str = "http://127.0.0.1:8011"
    CORS_ORIGINS: str = (
        "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174"
    )

    JWT_SECRET_KEY: str = Field(default="change-me-random-32-byte-hex")
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 360
    PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=15, ge=5, le=60)
    PLATFORM_SESSION_IDLE_TTL_SECONDS: int = Field(default=180, ge=60, le=3600)
    BROWSER_PRESENCE_HEARTBEAT_SECONDS: float = Field(default=15.0, ge=5.0, le=60.0)
    BROWSER_PRESENCE_RUNTIME_TOUCH_SECONDS: float = Field(default=60.0, ge=15.0, le=300.0)
    BROWSER_PRESENCE_FINAL_TOUCH_TIMEOUT_SECONDS: float = Field(default=5.0, ge=0.5, le=30.0)
    HERMES_AUTH_MODE: str = "dev"
    HERMES_ADMIN_AUTH_MODE: str = "inherit"
    # Contract-mode development may use the negative in-memory principal.
    # Any dev-auth flow backed by the real database must explicitly configure
    # an existing positive user id; the platform never seeds one implicitly.
    DEV_PRINCIPAL_USER_ID: int = -1
    # Used only to create the fixed `admin` account during the first password
    # mode database bootstrap. The plaintext is never persisted.
    ADMIN_INITIAL_PASSWORD: str = ""
    AES_ENCRYPTION_KEY: str = Field(default="change-me-random-16-byte-hex")
    RUNTIME_TOKEN_SECRET: str = Field(default="change-me-random-32-byte-hex")
    DEFAULT_ORG_ID: int = 1
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
