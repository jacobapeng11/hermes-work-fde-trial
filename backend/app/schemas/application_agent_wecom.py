from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ApplicationAgentWeComChannelUpsertRequest(BaseModel):
    name: str = Field(default="企业微信", min_length=1, max_length=100)
    corp_id: str = Field(min_length=2, max_length=128)
    wecom_agent_id: int = Field(gt=0)
    callback_token: str | None = Field(default=None, min_length=1, max_length=256)
    encoding_aes_key: str | None = Field(default=None, min_length=43, max_length=43)
    agent_secret: str | None = Field(default=None, min_length=1, max_length=512)
    enabled: bool = False

    @field_validator("corp_id", "callback_token", "encoding_aes_key", "agent_secret")
    @classmethod
    def strip_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ApplicationAgentWeComChannelSummary(BaseModel):
    channel_id: int
    application_agent_id: int
    name: str
    corp_id: str
    wecom_agent_id: int
    status: Literal["active", "disabled"]
    callback_url: str
    entry_url: str
    secrets_configured: bool
    updated_at: datetime | None = None


class ApplicationAgentWeComBindingRequest(BaseModel):
    ticket: str = Field(min_length=20, max_length=512)


class ApplicationAgentWeComBindingResponse(BaseModel):
    application_agent_name: str
    channel_name: str
    wecom_member_id_masked: str
    bound_at: datetime


class ApplicationAgentWeComBindingSummary(BaseModel):
    binding_id: int
    wecom_member_id_masked: str
    platform_user_id: int
    status: Literal["active", "revoked"]
    bound_at: datetime
    last_used_at: datetime | None = None
