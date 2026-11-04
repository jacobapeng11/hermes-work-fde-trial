from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ApplicationAgentAuthorizationRequest(BaseModel):
    application_agent: str = Field(
        min_length=2,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    client_id: str = Field(min_length=1, max_length=128)
    redirect_uri: str | None = Field(default=None, min_length=1, max_length=2_048)


class ApplicationAgentAuthorizationCodeResponse(BaseModel):
    code: str
    expires_in: int
    redirect_uri: str | None = None


class ApplicationAgentTokenRequest(BaseModel):
    grant_type: Literal["authorization_code"] = "authorization_code"
    code: str = Field(min_length=1, max_length=512)


class ApplicationAgentTrustedUserTokenRequest(BaseModel):
    user_code: str = Field(min_length=1, max_length=32)

    @field_validator("user_code")
    @classmethod
    def normalized_user_code(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user_code must not be blank")
        return normalized


class ApplicationAgentTokenIdentity(BaseModel):
    type: Literal["user"] = "user"
    user_code: str = Field(min_length=1, max_length=32)


class ApplicationAgentTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Agent"] = "Agent"
    expires_in: int
    identity: ApplicationAgentTokenIdentity
