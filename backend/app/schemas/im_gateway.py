from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ImProvider = Literal["feishu", "wecom", "wechat", "dingtalk"]
ImChannelStatus = Literal[
    "unconfigured",
    "connecting",
    "connected",
    "reconnecting",
    "disabled",
    "error",
    "revoked",
]


class ImChannelCard(BaseModel):
    provider: ImProvider
    display_name: str
    available: bool
    enabled: bool = False
    status: ImChannelStatus = "unconfigured"
    connection_mode: str | None = None
    bot_id: str | None = None
    secret_configured: bool = False
    runtime_session_id: str | None = None
    config_generation: int | None = None
    last_connected_at: datetime | None = None
    last_message_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


class ImChannelListResponse(BaseModel):
    channels: list[ImChannelCard]


class ImQrOnboardingCreated(BaseModel):
    onboarding_id: str
    qr_content: str
    expires_at: datetime
    poll_after_ms: int = 3000


class ImQrOnboardingStatus(BaseModel):
    onboarding_id: str
    status: Literal[
        "pending",
        "scanned",
        "credentials_ready",
        "consumed",
        "expired",
        "failed",
    ]
    expires_at: datetime
    bot_id: str | None = None
    secret_ready: bool = False
    error_code: str | None = None
    error_message: str | None = None


class RegisterWeComChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_mode: Literal["websocket", "url_callback"] = "websocket"
    bot_id: str | None = Field(default=None, min_length=1, max_length=255)
    secret: str | None = Field(default=None, min_length=1, max_length=2000)
    onboarding_id: str | None = Field(
        default=None, min_length=1, max_length=64
    )

    @model_validator(mode="after")
    def validate_credentials(self) -> "RegisterWeComChannelRequest":
        manual = bool(self.bot_id and self.secret)
        qrcode = bool(self.onboarding_id)
        if manual == qrcode:
            raise ValueError(
                "provide either bot_id + secret or onboarding_id"
            )
        if bool(self.bot_id) != bool(self.secret):
            raise ValueError("bot_id and secret must be provided together")
        return self


class SetImChannelEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RevokeImChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


class WeComChannelResponse(ImChannelCard):
    provider: Literal["wecom"] = "wecom"
    display_name: str = "企微"


class ApplicationWeComChannelResponse(BaseModel):
    channel_id: int | None = None
    application_agent_id: int
    provider: Literal["wecom"] = "wecom"
    target_type: Literal["application_agent"] = "application_agent"
    activation_allowed: bool = False
    enabled: bool = False
    status: ImChannelStatus | Literal[
        "credentials_invalid",
        "legacy_migration_required",
    ] = "unconfigured"
    connection_mode: Literal["websocket"] = "websocket"
    bot_id: str | None = None
    secret_configured: bool = False
    direct_enabled: bool = True
    group_enabled: bool = True
    group_trigger: Literal["mention_only"] = "mention_only"
    config_generation: int | None = None
    last_connected_at: datetime | None = None
    last_message_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    legacy_migration_required: bool = False


class ApplicationWeComSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direct_enabled: bool = True
    group_enabled: bool = True


class ApplicationWeComUserSummary(BaseModel):
    identity_id: int | None = None
    platform_user_id: int
    user_name: str
    user_code: str | None = None
    member_id_masked: str
    status: Literal["active", "revoked"]
    first_seen_at: datetime
    last_seen_at: datetime
    last_chat_type: Literal["direct", "group"]


class ApplicationWeComUserListResponse(BaseModel):
    users: list[ApplicationWeComUserSummary]
    total: int


class ApplicationWeComMessageSummary(BaseModel):
    inbound_message_id: int
    chat_type: Literal["direct", "group"]
    status: str
    identity_status: str
    sender_platform_user_id: int | None = None
    received_at: datetime
    first_provider_dispatch_at: datetime | None = None
    completed_at: datetime | None = None


class ApplicationWeComMessageListResponse(BaseModel):
    messages: list[ApplicationWeComMessageSummary]
