from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User
from app.services.admin_auth import AdminAuthorizationService, InMemoryAdminRoleStore
from app.services.auth import (
    AdminPasswordLoginService,
    AuthenticatedUser,
    AuthenticationError,
    SqlAlchemyAdminPasswordAuthStore,
    hash_password,
    is_password_hash_usable,
)


class _AdminStore:
    async def authenticate_admin(self, *, username: str, password: str) -> AuthenticatedUser:
        if username != "admin" or password != "managed-password":
            raise AuthenticationError("invalid credentials")
        return AuthenticatedUser(
            org_id=settings.DEFAULT_ORG_ID,
            user_id=settings.DEV_PRINCIPAL_USER_ID,
            name="Administrator",
            email=None,
            user_code="admin",
        )


@pytest.mark.asyncio
async def test_admin_password_login_requires_the_fixed_admin_account_and_role() -> None:
    service = AdminPasswordLoginService(
        _AdminStore(),
        AdminAuthorizationService(InMemoryAdminRoleStore()),
    )

    token, user = await service.login(username="admin", password="managed-password")

    assert token.count(".") == 2
    assert user.user_code == "admin"
    with pytest.raises(AuthenticationError, match="invalid credentials"):
        await service.login(username="other", password="managed-password")


def test_password_hash_format_check_rejects_unusable_values() -> None:
    assert is_password_hash_usable(hash_password("managed-password"))
    assert not is_password_hash_usable(None)
    assert not is_password_hash_usable("pbkdf2_sha256$not-a-number$salt$digest")


@pytest.mark.asyncio
async def test_sql_admin_password_store_short_circuits_non_admin_username() -> None:
    class _Database:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("non-admin usernames must not query users")

    store = SqlAlchemyAdminPasswordAuthStore(_Database())  # type: ignore[arg-type]

    with pytest.raises(AuthenticationError, match="invalid credentials"):
        await store.authenticate_admin(username="operator", password="managed-password")
