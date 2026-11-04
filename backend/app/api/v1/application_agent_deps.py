from __future__ import annotations

from fastapi import Depends, HTTPException, status
from app.api.v1.user.runtime_workspace import get_principal
from app.api.v1.application_agent_role_deps import get_application_agent_authorization_service
from app.services.application_agent_admin import (
    ApplicationAgentAdminAuthorizationError,
    ApplicationAgentAuthorizationService,
)
from app.services.runtime_workspace import Principal


async def get_application_agent_admin_principal(
    principal: Principal = Depends(get_principal),
    service: ApplicationAgentAuthorizationService = Depends(get_application_agent_authorization_service),
) -> Principal:
    try:
        return await service.require_admin(principal)
    except ApplicationAgentAdminAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="application_agent_admin_required",
        ) from exc
