from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.application_agent_admin import (
    ApplicationAgentAuthorizationService,
    InMemoryApplicationAgentAdminStore,
    SqlAlchemyApplicationAgentAdminStore,
)


application_agent_admin_memory_store = InMemoryApplicationAgentAdminStore()


async def get_application_agent_authorization_service(
    db: AsyncSession = Depends(get_db),
) -> ApplicationAgentAuthorizationService:
    if settings.RUNTIME_CHAT_MODE == "real" or settings.HERMES_AUTH_MODE in {"enterprise_idpAES", "oauth2"}:
        return ApplicationAgentAuthorizationService(SqlAlchemyApplicationAgentAdminStore(db))
    return ApplicationAgentAuthorizationService(application_agent_admin_memory_store)
