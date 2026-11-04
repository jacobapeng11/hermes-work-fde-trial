from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.services.admin_auth import AdminPrincipal
from app.services.admin_work_no import (
    AdminWorkNoAuthorizationError,
    AdminWorkNoAuthorizationService,
    InMemoryAdminWorkNoStore,
    SqlAlchemyAdminWorkNoStore,
)
from app.services.runtime_workspace import Principal

_admin_work_no_store = InMemoryAdminWorkNoStore()


async def get_admin_authorization_service(
    db: AsyncSession = Depends(get_db),
) -> AdminWorkNoAuthorizationService:
    if (
        settings.RUNTIME_CHAT_MODE != "real"
        and settings.HERMES_AUTH_MODE not in {"enterprise_idpAES", "oauth2"}
        and settings.HERMES_ADMIN_AUTH_MODE != "password"
    ):
        return AdminWorkNoAuthorizationService(_admin_work_no_store)
    return AdminWorkNoAuthorizationService(SqlAlchemyAdminWorkNoStore(db))


async def get_admin_principal(
    principal: Principal = Depends(get_principal),
    service: AdminWorkNoAuthorizationService = Depends(get_admin_authorization_service),
) -> AdminPrincipal:
    # Public dev can expose the admin shell before the independent admin
    # password flow is implemented. This is deliberately separate from the
    # user auth mode so an EnterpriseIdp user deployment does not become anonymous.
    if settings.HERMES_ADMIN_AUTH_MODE == "dev":
        return AdminPrincipal(org_id=principal.org_id, user_id=principal.user_id, roles=("admin",))
    try:
        return await service.require_admin(principal)
    except AdminWorkNoAuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
