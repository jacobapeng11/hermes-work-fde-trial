from __future__ import annotations

import pytest

from app.services.admin_auth import AdminAuthorizationError, AdminAuthorizationService
from app.services.runtime_workspace import Principal


class FakeAdminRoleStore:
    def __init__(self, roles: set[str]) -> None:
        self.roles = roles
        self.calls: list[dict] = []

    async def role_codes_for_user(self, *, org_id: int, user_id: int):
        self.calls.append({"org_id": org_id, "user_id": user_id})
        return self.roles


async def test_require_admin_returns_admin_principal_for_admin_role() -> None:
    store = FakeAdminRoleStore({"admin"})

    result = await AdminAuthorizationService(store).require_admin(Principal(org_id=7, user_id=9))

    assert result.org_id == 7
    assert result.user_id == 9
    assert result.roles == ("admin",)
    assert store.calls == [{"org_id": 7, "user_id": 9}]


async def test_require_admin_accepts_platform_admin_role_case_insensitively() -> None:
    result = await AdminAuthorizationService(FakeAdminRoleStore({"Platform_Admin"})).require_admin(
        Principal(org_id=7, user_id=9)
    )

    assert result.roles == ("Platform_Admin",)


async def test_require_admin_rejects_non_admin_role() -> None:
    with pytest.raises(AdminAuthorizationError, match="admin role is required"):
        await AdminAuthorizationService(FakeAdminRoleStore({"user"})).require_admin(
            Principal(org_id=7, user_id=9)
        )
