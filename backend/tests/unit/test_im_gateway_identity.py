from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import BigInteger, func, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.im_gateway.identity import (
    OrganizationWeComIdentityService,
    WeComIdentityError,
)
from app.models import (
    Organization,
    OrganizationWeComIdentity,
    User,
)
from app.models.base import Base
from app.services.runtime_workspace import Principal


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger, _compiler: object, **_kwargs: object
) -> str:
    return "INTEGER"


async def _database() -> tuple[
    Any, async_sessionmaker[AsyncSession]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Organization.__table__,
                User.__table__,
                OrganizationWeComIdentity.__table__,
            ],
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_identity_resolves_shared_qw_token_before_legacy_mapping(
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([
                Organization(id=10, code="org-10", name="测试企业"),
                User(
                    id=20,
                    org_id=10,
                    user_code="E0020",
                    name="张三",
                    status="active",
                    qw_token="wecom-zhangsan",
                ),
            ])
            await db.commit()

            service = OrganizationWeComIdentityService(db)
            resolved = await service.resolve(
                org_id=10,
                member_id="wecom-zhangsan",
            )

            assert resolved is not None
            assert resolved.identity_id is None
            assert resolved.principal == Principal(
                org_id=10,
                user_id=20,
            )
            assert resolved.user_name == "张三"
            assert resolved.user_code == "E0020"
            assert await db.scalar(
                select(func.count(OrganizationWeComIdentity.id))
            ) == 0
    finally:
        await engine.dispose()


class TestSharedBindingUrl:
    """Tests for shared_binding_url static method."""

    def test_uses_public_web_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """shared_binding_url should use PUBLIC_WEB_BASE, not hardcoded domain."""
        from app.im_gateway.identity import OrganizationWeComIdentityService

        monkeypatch.setattr(
            "app.im_gateway.identity.settings.PUBLIC_WEB_BASE",
            "https://agent-hermes-v2.gateway.example.com",
        )

        url = OrganizationWeComIdentityService.shared_binding_url(
            member_id="wojmppEQAAQH4eVzI-f-TqHvTykpU7lw"
        )

        assert url.startswith("https://agent-hermes-v2.gateway.example.com/")
        assert "/user/wecom-bind?" in url
        assert "qw_token=wojmppEQAAQH4eVzI-f-TqHvTykpU7lw" in url

    def test_does_not_use_cowork_hermes_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """shared_binding_url should NOT point to cowork-hermes."""
        from app.im_gateway.identity import OrganizationWeComIdentityService

        monkeypatch.setattr(
            "app.im_gateway.identity.settings.PUBLIC_WEB_BASE",
            "https://hermes.example.com",
        )

        url = OrganizationWeComIdentityService.shared_binding_url(
            member_id="test-member-id"
        )

        assert "agent-hermes.gateway.example.com" not in url
        assert url.startswith("https://hermes.example.com/user/wecom-bind?")


class TestBindQwToken:
    """Tests for bind_qw_token service method."""

    @pytest.mark.asyncio
    async def test_bind_sets_qw_token_on_user(self) -> None:
        engine, sessions = await _database()
        try:
            async with sessions() as db:
                db.add_all([
                    Organization(id=10, code="org-10", name="测试企业"),
                    User(
                        id=20,
                        org_id=10,
                        user_code="E0020",
                        name="张三",
                        status="active",
                    ),
                ])
                await db.commit()

                service = OrganizationWeComIdentityService(db)
                await service.bind_qw_token(
                    principal=Principal(org_id=10, user_id=20),
                    qw_token="wecom-zhangsan",
                )

                user = await db.get(User, 20)
                assert user is not None
                assert user.qw_token == "wecom-zhangsan"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_bind_is_idempotent_for_same_user(self) -> None:
        engine, sessions = await _database()
        try:
            async with sessions() as db:
                db.add_all([
                    Organization(id=10, code="org-10", name="测试企业"),
                    User(
                        id=20,
                        org_id=10,
                        user_code="E0020",
                        name="张三",
                        status="active",
                        qw_token="wecom-zhangsan",
                    ),
                ])
                await db.commit()

                service = OrganizationWeComIdentityService(db)
                # Binding the same qw_token again should not raise
                await service.bind_qw_token(
                    principal=Principal(org_id=10, user_id=20),
                    qw_token="wecom-zhangsan",
                )

                user = await db.get(User, 20)
                assert user is not None
                assert user.qw_token == "wecom-zhangsan"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_bind_conflicts_when_another_user_owns_qw_token(self) -> None:
        engine, sessions = await _database()
        try:
            async with sessions() as db:
                db.add_all([
                    Organization(id=10, code="org-10", name="测试企业"),
                    User(
                        id=20,
                        org_id=10,
                        user_code="E0020",
                        name="张三",
                        status="active",
                        qw_token="wecom-zhangsan",
                    ),
                    User(
                        id=21,
                        org_id=10,
                        user_code="E0021",
                        name="李四",
                        status="active",
                    ),
                ])
                await db.commit()

                service = OrganizationWeComIdentityService(db)
                with pytest.raises(
                    WeComIdentityError,
                    match="wecom_identity_qw_token_conflict",
                ):
                    await service.bind_qw_token(
                        principal=Principal(org_id=10, user_id=21),
                        qw_token="wecom-zhangsan",
                    )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_bind_rejects_empty_qw_token(self) -> None:
        engine, sessions = await _database()
        try:
            async with sessions() as db:
                db.add_all([
                    Organization(id=10, code="org-10", name="测试企业"),
                    User(
                        id=20,
                        org_id=10,
                        user_code="E0020",
                        name="张三",
                        status="active",
                    ),
                ])
                await db.commit()

                service = OrganizationWeComIdentityService(db)
                with pytest.raises(
                    WeComIdentityError,
                    match="wecom_identity_qw_token_empty",
                ):
                    await service.bind_qw_token(
                        principal=Principal(org_id=10, user_id=20),
                        qw_token="   ",
                    )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_bind_rejects_qw_token_longer_than_database_contract(self) -> None:
        engine, sessions = await _database()
        try:
            async with sessions() as db:
                db.add_all([
                    Organization(id=10, code="org-10", name="测试企业"),
                    User(
                        id=20,
                        org_id=10,
                        user_code="E0020",
                        name="张三",
                        status="active",
                    ),
                ])
                await db.commit()

                service = OrganizationWeComIdentityService(db)
                with pytest.raises(
                    WeComIdentityError,
                    match="wecom_identity_qw_token_invalid",
                ):
                    await service.bind_qw_token(
                        principal=Principal(org_id=10, user_id=20),
                        qw_token="x" * 101,
                    )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_bind_rejects_user_from_another_org_or_inactive_user(self) -> None:
        engine, sessions = await _database()
        try:
            async with sessions() as db:
                db.add_all([
                    Organization(id=10, code="org-10", name="企业一"),
                    Organization(id=11, code="org-11", name="企业二"),
                    User(
                        id=20,
                        org_id=10,
                        user_code="E0020",
                        name="张三",
                        status="active",
                    ),
                    User(
                        id=21,
                        org_id=11,
                        user_code="E0021",
                        name="李四",
                        status="active",
                    ),
                    User(
                        id=22,
                        org_id=10,
                        user_code="E0022",
                        name="王五",
                        status="disabled",
                    ),
                ])
                await db.commit()

                service = OrganizationWeComIdentityService(db)
                for principal in (
                    Principal(org_id=10, user_id=21),
                    Principal(org_id=10, user_id=22),
                ):
                    with pytest.raises(
                        WeComIdentityError,
                        match="wecom_identity_user_not_found",
                    ):
                        await service.bind_qw_token(
                            principal=principal,
                            qw_token="wecom-user",
                        )
        finally:
            await engine.dispose()
