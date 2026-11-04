from __future__ import annotations

import json
from ipaddress import ip_address
from urllib.parse import urlparse

from pydantic import field_validator, model_validator


class ValidationSettings:
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
    def validate_production_auth_boundary(self) -> "ValidationSettings":
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
