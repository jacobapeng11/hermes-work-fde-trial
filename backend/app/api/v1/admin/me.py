from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.admin.deps import get_admin_principal
from app.schemas.admin import AdminPrincipalSummary
from app.services.admin_auth import AdminPrincipal

router = APIRouter(prefix="/me", tags=["admin-me"])


@router.get("", response_model=AdminPrincipalSummary)
async def get_admin_me(principal: AdminPrincipal = Depends(get_admin_principal)) -> AdminPrincipalSummary:
    return AdminPrincipalSummary(
        org_id=principal.org_id,
        user_id=principal.user_id,
        roles=list(principal.roles),
    )
