from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SupervisorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    RUNTIME_ID: str = Field(min_length=1, max_length=128)
    RUNTIME_BINDING_ID: str = Field(min_length=1, max_length=128)
    RUNTIME_MODE: Literal["personal", "application"]
    API_SERVER_KEY: SecretStr = Field(min_length=32)
    NEWAPI_API_KEY: SecretStr = Field(min_length=32)
    NEWAPI_BASE_URL: str = Field(min_length=1, max_length=2048)
    BACKEND_INTERNAL_BASE_URL: str = Field(
        default="http://backend:8011",
        min_length=1,
        max_length=2048,
    )
    RUNTIME_CONFIG_REVISION: str = Field(default="0", min_length=1, max_length=128)
    RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
    )
    RUNTIME_INITIAL_CONFIG_DIGEST: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    RUNTIME_DATA_ROOT: Path = Path("/data/platform")
    RUNTIME_EDGE_HOST: Literal["0.0.0.0"] = "0.0.0.0"
    RUNTIME_EDGE_PORT: Literal[9120] = 9120
    HERMES_DASHBOARD_HOST: Literal["127.0.0.1"] = "127.0.0.1"
    HERMES_DASHBOARD_PORT: Literal[9119] = 9119
    MODEL_RELAY_HOST: Literal["127.0.0.1"] = "127.0.0.1"
    MODEL_RELAY_PORT: Literal[9121] = 9121
    HERMES_EXECUTABLE: Path = Path("/opt/hermes/.venv/bin/hermes")
    HERMES_CHILD_EXECUTABLE: Path = Path("/opt/hermes/.venv/bin/platform-hermes-child-exec")
    HERMES_HOME: Path = Path("/opt/data")
    HERMES_CHILD_UID: Literal[10001] = 10001
    HERMES_CHILD_GID: Literal[10001] = 10001
    HERMES_WORKSPACE_GID: int = Field(default=1000, gt=0, le=2_147_483_647)
    # Environment variables are strings.  Keep this as an integer field and
    # enforce the closed pair below; Literal[int] rejects the image's canonical
    # ``ENV HERMES_EXPECTED_CONFIG_SCHEMA=33|34`` before settings validation.
    HERMES_EXPECTED_CONFIG_SCHEMA: int = Field(default=33, ge=33, le=34)
    HERMES_STARTUP_MODE: Literal["steady_state", "fresh_probe", "fresh_bootstrap"]
    RUNTIME_BOOTSTRAP_INTENT_ID: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    RUNTIME_BOOTSTRAP_AUTHORIZATION_DIGEST: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    HERMES_LEGACY_SOURCE_GATE_ENABLED: bool = False
    HERMES_FRESH_STORAGE_PROBE_ENABLED: bool = False
    HERMES_SCHEMA_SCRATCH_ROOT: Path = Path("/tmp/hermes-schema")
    PLATFORM_CONTROL_KEY_FILE: Path = Path("/run/secrets/hermes-platform-control/control-keys.json")
    PLATFORM_SCHEMA_EXPECTATION_FILE: Path = Path(
        "/run/config/hermes-schema-startup/expectation.json"
    )
    RUNTIME_PVC_STORAGE_IDENTITY_HASH: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    HERMES_SUPERVISOR_POD_UID: str | None = Field(
        default=None,
        min_length=8,
        max_length=256,
    )
    HERMES_KNATIVE_REVISION_NAME: str | None = Field(
        default=None,
        min_length=8,
        max_length=256,
    )
    HERMES_FTS5_CJK_SO: Literal["/opt/hermes-platform/lib/libfts5_cjk.so"] = (
        "/opt/hermes-platform/lib/libfts5_cjk.so"
    )
    WORKSPACE_DIR: Path = Path("/workspace")
    HERMES_KANBAN_ORCHESTRATOR_ENABLED: bool = False
    HERMES_KANBAN_DISPATCH_INTERVAL_SECONDS: int = Field(
        default=5,
        ge=1,
        le=300,
    )
    HERMES_STARTUP_TIMEOUT_SECONDS: int = Field(default=120, ge=5, le=600)

    APPLICATION_AGENT_MAX_INFLIGHT_RUNS: int = Field(default=500, ge=1, le=10_000)
    APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS: int = Field(
        default=600,
        ge=1,
        le=3600,
    )
    APPLICATION_AGENT_HOT_SESSION_LIMIT: int = Field(default=64, ge=0, le=10_000)
    APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS: int = Field(
        default=900,
        ge=1,
        le=7200,
    )
    PERSONAL_MAX_INFLIGHT_RUNS: int = Field(default=10, ge=1, le=10)
    PERSONAL_MAX_RESIDENT_SESSIONS: int = Field(default=14, ge=1, le=64)

    DISPATCH_GROUP_COMMIT_WINDOW_MS: Literal[2] = 2
    SQLITE_SYNCHRONOUS: Literal["FULL"] = "FULL"
    SQLITE_BUSY_TIMEOUT_MS: int = Field(default=5000, ge=100, le=60_000)
    PROJECTION_POLL_INTERVAL_SECONDS: float = Field(default=0.25, ge=0.05, le=10)
    PROJECTION_MAX_BACKOFF_SECONDS: float = Field(default=30, ge=1, le=300)
    MODEL_ROUTE_TTL_SECONDS: int = Field(default=3900, ge=600, le=7200)
    PROVIDER_ROUTE_ACCEPTANCE_MODE: Literal["compatible", "required"] = "compatible"
    POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS: int = Field(
        default=30,
        ge=1,
        le=300,
    )
    POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS: int = Field(
        default=15,
        ge=1,
        le=300,
    )
    POST_TURN_REVIEW_MAX_DURATION_SECONDS: int = Field(
        default=300,
        ge=1,
        le=1800,
    )
    POST_TURN_REVIEW_MAX_OUTPUT_TOKENS: int = Field(
        default=8192,
        ge=1,
        le=1_000_000,
    )
    MIN_NOFILE: Literal[65536] = 65536
    MIN_PIDS: Literal[4096] = 4096

    @model_validator(mode="after")
    def validate_warm_policy(self) -> "SupervisorSettings":
        bootstrap_authorized = bool(
            self.RUNTIME_BOOTSTRAP_INTENT_ID and self.RUNTIME_BOOTSTRAP_AUTHORIZATION_DIGEST
        )
        if bool(self.RUNTIME_BOOTSTRAP_INTENT_ID) != bool(
            self.RUNTIME_BOOTSTRAP_AUTHORIZATION_DIGEST
        ):
            raise ValueError("Runtime bootstrap intent and digest must be configured together")
        if self.HERMES_STARTUP_MODE == "fresh_probe" and (
            bootstrap_authorized
            or self.RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL
            or self.RUNTIME_INITIAL_CONFIG_DIGEST
        ):
            raise ValueError("fresh probe cannot receive bootstrap or ConfigBundle inputs")
        if self.HERMES_STARTUP_MODE == "fresh_bootstrap" and (
            not bootstrap_authorized
            or not self.RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL
            or not self.RUNTIME_INITIAL_CONFIG_DIGEST
        ):
            raise ValueError("fresh bootstrap authorization and ConfigBundle are required")
        if self.HERMES_STARTUP_MODE == "steady_state" and bootstrap_authorized:
            raise ValueError("steady state cannot receive bootstrap authorization")
        if self.HERMES_LEGACY_SOURCE_GATE_ENABLED and self.HERMES_EXPECTED_CONFIG_SCHEMA != 33:
            raise ValueError("legacy source gate requires Hermes config schema 33")
        if self.HERMES_FRESH_STORAGE_PROBE_ENABLED and self.HERMES_EXPECTED_CONFIG_SCHEMA != 34:
            raise ValueError("fresh storage probe requires Hermes config schema 34")
        if self.HERMES_FRESH_STORAGE_PROBE_ENABLED and self.HERMES_LEGACY_SOURCE_GATE_ENABLED:
            raise ValueError("fresh storage probe and legacy source gate are exclusive")
        if bool(self.RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL) != bool(
            self.RUNTIME_INITIAL_CONFIG_DIGEST
        ):
            raise ValueError("Runtime initial config URL and digest must be configured together")
        if (
            self.APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS
            > self.APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS
        ):
            raise ValueError(
                "APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS cannot exceed "
                "APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS"
            )
        if self.PERSONAL_MAX_RESIDENT_SESSIONS < self.PERSONAL_MAX_INFLIGHT_RUNS:
            raise ValueError(
                "PERSONAL_MAX_RESIDENT_SESSIONS cannot be lower than PERSONAL_MAX_INFLIGHT_RUNS"
            )
        parsed_newapi = urlparse(self.NEWAPI_BASE_URL)
        if parsed_newapi.scheme not in {"http", "https"} or not parsed_newapi.netloc:
            raise ValueError("NEWAPI_BASE_URL must be an absolute HTTP(S) URL")
        if (
            parsed_newapi.username
            or parsed_newapi.password
            or parsed_newapi.query
            or parsed_newapi.fragment
        ):
            raise ValueError("NEWAPI_BASE_URL cannot contain credentials, query, or fragment")
        parsed_backend = urlparse(self.BACKEND_INTERNAL_BASE_URL)
        if (
            parsed_backend.scheme not in {"http", "https"}
            or not parsed_backend.netloc
            or parsed_backend.username
            or parsed_backend.password
            or parsed_backend.query
            or parsed_backend.fragment
        ):
            raise ValueError(
                "BACKEND_INTERNAL_BASE_URL must be an absolute HTTP(S) URL "
                "without credentials, query, or fragment"
            )
        return self

    @computed_field
    @property
    def max_inflight_runs(self) -> int:
        if self.RUNTIME_MODE == "application":
            return self.APPLICATION_AGENT_MAX_INFLIGHT_RUNS
        return self.PERSONAL_MAX_INFLIGHT_RUNS

    @computed_field
    @property
    def max_resident_sessions(self) -> int:
        if self.RUNTIME_MODE == "application":
            return (
                self.APPLICATION_AGENT_MAX_INFLIGHT_RUNS + self.APPLICATION_AGENT_HOT_SESSION_LIMIT
            )
        return self.PERSONAL_MAX_RESIDENT_SESSIONS

    @computed_field
    @property
    def max_external_websockets(self) -> int:
        if self.RUNTIME_MODE == "application":
            return self.APPLICATION_AGENT_MAX_INFLIGHT_RUNS * 4
        return 4

    @computed_field
    @property
    def model_relay_max_connections(self) -> int:
        return max(1, (self.max_inflight_runs * 12 + 9) // 10)

    @computed_field
    @property
    def model_relay_max_keepalive_connections(self) -> int:
        if self.RUNTIME_MODE == "application":
            return self.APPLICATION_AGENT_HOT_SESSION_LIMIT * 2
        return 2

    @computed_field
    @property
    def supervisor_database_path(self) -> Path:
        return self.RUNTIME_DATA_ROOT / "supervisor.db"
