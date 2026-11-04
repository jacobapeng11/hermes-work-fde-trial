from __future__ import annotations

import pytest

from app.services.admin_work_no import (
    AdminWorkNoAuthorizationError,
    AdminWorkNoAuthorizationService,
    InMemoryAdminWorkNoStore,
)
from app.services.runtime_workspace import Principal


@pytest.mark.asyncio
async def test_admin_work_number_authorization_is_scoped_by_organization_and_status() -> None:
    store = InMemoryAdminWorkNoStore(
        active_work_nos={(1, "10001")},
        user_work_nos={(1, 10): "10001", (2, 10): "10001", (1, 11): "10002"},
    )
    service = AdminWorkNoAuthorizationService(store)

    assert await service.is_admin(Principal(org_id=1, user_id=10)) is True
    assert await service.is_admin(Principal(org_id=2, user_id=10)) is False
    assert await service.is_admin(Principal(org_id=1, user_id=11)) is False


@pytest.mark.asyncio
async def test_admin_work_number_authorization_uses_trimmed_work_number_and_rejects_missing_values() -> None:
    service = AdminWorkNoAuthorizationService(
        InMemoryAdminWorkNoStore(active_work_nos={(1, "10002")})
    )

    assert await service.is_admin_work_no(org_id=1, work_no=" 10002 ") is True
    assert await service.is_admin_work_no(org_id=1, work_no=None) is False
    with pytest.raises(
        AdminWorkNoAuthorizationError,
        match="admin work number permission is required",
    ):
        await service.require_admin(Principal(org_id=1, user_id=99))
