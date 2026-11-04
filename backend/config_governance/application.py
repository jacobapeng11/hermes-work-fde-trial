from __future__ import annotations

from typing import Literal

from pydantic import Field


class ApplicationSettings:
    APPLICATION_AGENT_AUTHORIZATION_CODE_TTL_SECONDS: int = Field(default=120, ge=30, le=600)
    APPLICATION_AGENT_TOKEN_TTL_SECONDS: int = Field(default=600, ge=60, le=3600)
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
    APPLICATION_AGENT_MAX_INFLIGHT_RUNS: int = Field(default=500, ge=1, le=10_000)
    APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS: int = Field(default=600, ge=1, le=3600)
    APPLICATION_AGENT_HOT_SESSION_LIMIT: int = Field(default=64, ge=0, le=10_000)
    APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS: int = Field(default=900, ge=1, le=7200)
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
