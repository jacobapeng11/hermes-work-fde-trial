from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

import jwt


@dataclass(frozen=True)
class KnowledgeIdentity:
    org_id: int
    user_id: int
    user_code: str
    runtime_workspace_id: int
    application_agent_id: int | None = None
    application_client_id: int | None = None
    runtime_run_id: str | None = None
    service_identity: bool = False


class KnowledgeAssertionIssuer:
    def __init__(self, *, issuer: str, ttl_seconds: int) -> None:
        self.issuer = issuer
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        identity: KnowledgeIdentity,
        audience: str,
        scope: str,
        secret: str,
    ) -> str:
        if not secret:
            raise ValueError("knowledge assertion secret is not configured")
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "iss": self.issuer,
            "aud": audience,
            "sub": str(identity.user_id),
            "org_id": identity.org_id,
            "user_code": identity.user_code,
            "runtime_workspace_id": identity.runtime_workspace_id,
            "scope": scope,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + self.ttl_seconds,
        }
        if identity.application_agent_id is not None:
            claims["application_agent_id"] = identity.application_agent_id
        if identity.application_client_id is not None:
            claims["application_client_id"] = identity.application_client_id
        if identity.runtime_run_id is not None:
            claims["runtime_run_id"] = identity.runtime_run_id
        if identity.service_identity:
            claims["service_identity"] = True
        return jwt.encode(
            claims,
            secret,
            algorithm="HS256",
        )
