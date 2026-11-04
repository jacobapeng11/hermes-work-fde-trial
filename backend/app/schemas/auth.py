from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    identifier: str
    password: str
    org_code: str = "default"


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class EnterpriseIdpTokenExchangeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class WeComBindRequest(BaseModel):
    qw_token: str = Field(min_length=1, max_length=100)


class AuthPrincipalSummary(BaseModel):
    org_id: int
    user_id: int
    name: str
    email: str | None = None
    user_code: str | None = None
    can_manage_application_agents: bool = False
    is_admin: bool = False


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int | None = None
    principal: AuthPrincipalSummary
