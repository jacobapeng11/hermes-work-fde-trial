from __future__ import annotations

from typing import Literal

from pydantic import Field


class RuntimeSettings:
    HERMES_RUNTIME_NAMESPACE: str = "hermes-runtimes"
    # Runtime image selection is read directly from backend/.env by
    # RuntimeImageConfigStore so long-lived processes observe atomic upgrades
    # without caching the active image in Settings.
    HERMES_PLATFORM_REVISION: str = "workspace"
    HERMES_RUNTIME_SERVICE_ACCOUNT: str = "hermes-runtime-workload"
    HERMES_RUNTIME_INGRESS_BASE_URL: str = "http://127.0.0.1:31080"
    RUNTIME_EDGE_MAX_PROMPT_BYTES: int = Field(default=1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    RUNTIME_EDGE_MAX_OUTPUT_TOKENS: int = Field(default=32768, ge=1, le=1_000_000)
    PROVIDER_ROUTE_ISSUANCE_MODE: Literal["off", "canary", "on"] = "off"
    PROVIDER_ROUTE_CANARY_RUNTIME_IDS: str = ""
    RUNTIME_PROVIDER_ROUTE_ACCEPTANCE_MODE: Literal["compatible", "required"] = "compatible"

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

    MCP_RUNTIME_IDENTITY_MODE: Literal["legacy_slug", "canary", "asset_id"] = "asset_id"
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
    HERMES_RUNTIME_NODE_NAME: str = "demo"
    HERMES_RUNTIME_STORAGE_MAINTENANCE: bool = False
    ENTERPRISE_KNOWLEDGE_HOST_PATH: str = ""
    HERMES_RUNTIME_IDLE_TTL_SECONDS: int = Field(default=600, ge=1)
    HERMES_RUNTIME_READY_TIMEOUT_SECONDS: int = 120
    HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS: int = 3900
    # Legacy clarify expiry; new requests use the Backend auto-default trigger.
    HERMES_USER_WEB_CLARIFY_TIMEOUT_SECONDS: int = Field(default=3600, ge=-1, le=86_400)
    # Auto-default remains a Backend-owned asynchronous trigger; it is not part
    # of the normal chat submit's first-token path.
    HERMES_USER_WEB_CLARIFY_AUTO_DEFAULT_SECONDS: int = Field(default=60, ge=-1, le=86_400)
    HERMES_USER_WEB_CLARIFY_SCAN_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=3_600)
    HERMES_USER_WEB_CLARIFY_SCAN_BATCH_SIZE: int = Field(default=25, ge=1, le=100)
    HERMES_USER_WEB_CLARIFY_CLAIM_LEASE_SECONDS: int = Field(default=120, ge=90, le=3_600)
    HERMES_USER_WEB_CLARIFY_MAX_AUTO_RESUME_ATTEMPTS: int = Field(default=12, ge=1, le=1_000)
    HERMES_DASHBOARD_HOT_CHANNEL_IDLE_GRACE_SECONDS: int = Field(default=90, ge=30, le=300)
    HERMES_TUI_WS_ORPHAN_REAP_GRACE_S: int = Field(default=900, ge=1)
    KUBECONFIG: str = ""
    # Dedicated least-privilege identity for the Admin resource console.
    # Never silently fall back to the Runtime provisioning kubeconfig.
    ADMIN_KUBECONFIG: str = ""
    NEWAPI_BASE_URL: str = "http://newapi:3000/v1"
    NEWAPI_API_KEY_SECRET_NAME: str = "hermes-newapi-runtime"
    RUNTIME_CHAT_MODE: str = "contract"
    # Web chat has one admission contract. Keeping this as a literal makes
    # stale optional/disabled deployment configuration fail at startup
    # instead of silently re-enabling the legacy slow path.
    RUNTIME_WEB_READY_RECEIPT_MODE: Literal["required"] = "required"
    RUNTIME_PREPARED_KNOWLEDGE_TTL_SECONDS: int = Field(default=180, ge=90, le=3600)
    RUNTIME_RUN_RECONCILE_AFTER_SECONDS: int = 300
    RUNTIME_RUN_MAX_SECONDS: int = 3600
    RUNTIME_RUN_CANCEL_GRACE_SECONDS: int = 30
    RUNTIME_RUN_RECONCILE_INTERVAL_SECONDS: int = 60
    RUNTIME_SCHEMA_STARTUP_RECONCILE_INTERVAL_SECONDS: int = 5
    RUNTIME_RUN_EVENT_RETENTION_SECONDS: int = 86400
