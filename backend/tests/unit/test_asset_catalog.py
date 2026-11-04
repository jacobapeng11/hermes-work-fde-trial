from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import Base, Mcp, McpSlugLease, McpVersion, Skill, SkillVersion
from app.schemas.asset_catalog import (
    CreateMcpAssetCommand,
    CreateSkillAssetCommand,
    McpVersionMaterial,
    PublishMcpVersionCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.services.asset_catalog import AssetCatalogError, AssetCatalogService


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "TEXT"


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _skill_material(marker: str) -> SkillVersionMaterial:
    return SkillVersionMaterial(
        package_uri=f"/data/assets/{marker}",
        package_sha256=marker * 64,
        manifest={"marker": marker},
    )


def _create_skill(*, owner_user_id: int = 2) -> CreateSkillAssetCommand:
    return CreateSkillAssetCommand(
        org_id=1,
        owner_user_id=owner_user_id,
        slug="report-writer",
        name="Report Writer",
        asset_scope="public",
        visibility="organization",
        material=_skill_material("a"),
    )


def _mcp_material(*, tool_name: str = "crm.search") -> McpVersionMaterial:
    return McpVersionMaterial(
        transport="streamable_http",
        connection_config={"url": "https://mcp.example.com/mcp"},
        secret_refs={"authorization": "Bearer test-token"},
        tools_schema=[{"name": tool_name}],
    )


@pytest.mark.asyncio
async def test_skill_versions_are_immutable_idempotent_and_rollbackable() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        first = await service.create_skill(_create_skill())
        second = await service.publish_skill_version(
            PublishSkillVersionCommand(
                org_id=1,
                skill_id=first.asset_id,
                material=_skill_material("b"),
            )
        )
        duplicate = await service.publish_skill_version(
            PublishSkillVersionCommand(
                org_id=1,
                skill_id=first.asset_id,
                material=_skill_material("b"),
            )
        )
        rollback = await service.publish_skill_version(
            PublishSkillVersionCommand(
                org_id=1,
                skill_id=first.asset_id,
                material=_skill_material("a"),
            )
        )
        await session.commit()

        skill = await session.get(Skill, first.asset_id)
        version_one = await session.get(SkillVersion, first.version_id)
        assert first.version == 1 and second.version == 2
        assert duplicate.created_version is False
        assert rollback.created_version is False
        assert skill is not None and skill.current_version_id == first.version_id
        assert version_one is not None and version_one.package_uri == "/data/assets/a"
        assert await session.scalar(select(func.count(SkillVersion.id))) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_public_skill_slug_is_unique_across_publishers_in_one_org() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        await service.create_skill(_create_skill(owner_user_id=2))

        with pytest.raises(AssetCatalogError) as error:
            await service.create_skill(_create_skill(owner_user_id=9))
        assert error.value.code == "SKILL_SLUG_EXISTS"
    await engine.dispose()


@pytest.mark.asyncio
async def test_system_skill_names_are_reserved_for_public_and_personal_assets() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        for asset_scope, visibility in (("public", "organization"), ("personal", "private")):
            command = _create_skill().model_copy(update={
                "slug": "hermes-agent",
                "name": "Hermes Agent",
                "asset_scope": asset_scope,
                "visibility": visibility,
            })
            with pytest.raises(AssetCatalogError) as error:
                await service.create_skill(command)
            assert error.value.code == "SKILL_SYSTEM_RESERVED"
    await engine.dispose()


def test_skill_catalog_rejects_retired_frozen_scope() -> None:
    with pytest.raises(ValidationError):
        CreateSkillAssetCommand(
            org_id=1,
            owner_user_id=9,
            slug="expert-701-skill-201",
            name="Retired Frozen Report Writer",
            asset_scope="expert_frozen",
            visibility="private",
            material=SkillVersionMaterial(
                package_uri="/data/expert-artifacts/701/skill-201",
                package_sha256="c" * 64,
            ),
        )


def test_skill_material_rejects_retired_frozen_backlinks() -> None:
    with pytest.raises(ValidationError):
        SkillVersionMaterial(
            package_uri="/data/expert-artifacts/701/skill-201",
            package_sha256="c" * 64,
            source_skill_id=101,
        )


def test_mcp_material_rejects_plaintext_credentials() -> None:
    with pytest.raises(ValidationError, match="non-secret settings"):
        McpVersionMaterial(
            transport="streamable_http",
            connection_config={
                "url": "https://mcp.example.com",
                "headers": {"Authorization": "Bearer plaintext"},
            },
        )


@pytest.mark.parametrize(
    ("transport", "connection_config"),
    [
        ("streamable_http", {}),
        ("sse", {}),
        ("streamable_http", {"url": "ftp://mcp.example.com/mcp"}),
        ("streamable_http", {"url": "https://mcp.example.com:invalid/mcp"}),
        ("stdio", {}),
        ("stdio", {"command": " "}),
    ],
)
def test_mcp_material_rejects_incomplete_connection_target(
    transport: str,
    connection_config: dict,
) -> None:
    with pytest.raises(ValidationError, match="connection target"):
        McpVersionMaterial(
            transport=transport,
            connection_config=connection_config,
        )


def test_mcp_material_accepts_required_connection_targets() -> None:
    http = McpVersionMaterial(
        transport="sse",
        connection_config={"url": "https://mcp.example.com/sse"},
    )
    stdio = McpVersionMaterial(
        transport="stdio",
        connection_config={"command": "crm-mcp"},
    )

    assert http.connection_config["url"].startswith("https://")
    assert stdio.connection_config["command"] == "crm-mcp"


@pytest.mark.parametrize(
    ("transport", "connection_config", "target"),
    [
        (
            "streamable_http",
            {"url": "https://mcp.example.com", "headers": ["invalid"]},
            "headers",
        ),
        (
            "stdio",
            {"command": "crm-mcp", "env": ["invalid"]},
            "env",
        ),
    ],
)
def test_mcp_material_rejects_invalid_credential_target(
    transport: str,
    connection_config: dict,
    target: str,
) -> None:
    with pytest.raises(ValidationError, match=target):
        McpVersionMaterial(
            transport=transport,
            connection_config=connection_config,
            secret_refs={"API_KEY": "test-token"},
        )


@pytest.mark.asyncio
async def test_mcp_schema_change_creates_version_without_secret_on_head() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        first = await service.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=2,
                slug="crm",
                name="CRM",
                visibility="organization",
                material=_mcp_material(),
            )
        )
        second = await service.publish_mcp_version(
            PublishMcpVersionCommand(
                org_id=1,
                mcp_id=first.asset_id,
                material=_mcp_material(tool_name="crm.create_follow_up"),
            )
        )
        await session.commit()

        mcp = await session.get(Mcp, first.asset_id)
        version = await session.get(McpVersion, second.version_id)
        assert first.content_digest != second.content_digest
        assert mcp is not None and mcp.config == {}
        assert version is not None
        assert version.secret_refs == {"authorization": "Bearer test-token"}
        assert await session.scalar(select(func.count(McpVersion.id))) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_update_preserves_asset_identity_and_checks_expected_version() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        first = await service.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=2,
                slug="crm",
                name="CRM",
                visibility="organization",
                material=_mcp_material(),
            )
        )
        second = await service.publish_mcp_version(
            PublishMcpVersionCommand(
                org_id=1,
                mcp_id=first.asset_id,
                expected_current_version_id=first.version_id,
                name="CRM v2",
                description="Updated endpoint",
                material=McpVersionMaterial(
                    transport="streamable_http",
                    connection_config={"url": "https://mcp-v2.example.com/mcp"},
                    tools_schema=[{"name": "crm.search"}],
                ),
            )
        )

        with pytest.raises(AssetCatalogError) as error:
            await service.publish_mcp_version(
                PublishMcpVersionCommand(
                    org_id=1,
                    mcp_id=first.asset_id,
                    expected_current_version_id=first.version_id,
                    name="stale update",
                    material=_mcp_material(tool_name="crm.stale"),
                )
            )

        mcp = await session.get(Mcp, first.asset_id)
        assert second.asset_id == first.asset_id
        assert second.version == 2
        assert error.value.code == "MCP_VERSION_CONFLICT"
        assert mcp is not None and mcp.name == "CRM v2"
        assert mcp.description == "Updated endpoint"
    await engine.dispose()


@pytest.mark.asyncio
async def test_revoked_mcp_releases_slug_without_inheriting_asset_identity() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        first = await service.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=2,
                slug="crm",
                name="CRM old",
                visibility="organization",
                material=_mcp_material(),
            )
        )
        await service.revoke_mcp(org_id=1, mcp_id=first.asset_id)
        second = await service.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=9,
                slug="crm",
                name="CRM new",
                visibility="organization",
                material=McpVersionMaterial(
                    transport="streamable_http",
                    connection_config={"url": "https://new.example.com/mcp"},
                ),
            )
        )
        await session.commit()

        old = await session.get(Mcp, first.asset_id)
        lease = await session.scalar(select(McpSlugLease))
        assert old is not None and old.status == "revoked" and old.slug == "crm"
        assert second.asset_id != first.asset_id
        assert lease is not None and lease.mcp_id == second.asset_id
        assert lease.slug == "crm"
        assert await session.scalar(select(func.count(McpVersion.id))) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_execution_toggle_normalizes_legacy_archive_and_preserves_slug_lease() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        created = await service.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=2,
                slug="crm",
                name="CRM",
                visibility="organization",
                material=_mcp_material(),
            )
        )

        await service.archive_mcp(org_id=1, mcp_id=created.asset_id)
        disabled = await service.set_mcp_execution_enabled(
            org_id=1,
            mcp_id=created.asset_id,
            enabled=False,
        )
        assert disabled.status == "published"
        assert disabled.archived_at is None
        assert disabled.revoked_at is not None
        lease = await session.scalar(
            select(McpSlugLease).where(McpSlugLease.mcp_id == created.asset_id)
        )
        assert lease is not None and lease.slug == "crm"

        enabled = await service.set_mcp_execution_enabled(
            org_id=1,
            mcp_id=created.asset_id,
            enabled=True,
        )
        await session.commit()
        assert enabled.status == "published"
        assert enabled.archived_at is None
        assert enabled.revoked_at is None
        assert await session.scalar(
            select(func.count(McpSlugLease.id)).where(
                McpSlugLease.mcp_id == created.asset_id
            )
        ) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_slug_recreate_stays_blocked_before_full_identity_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        first = await service.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=2,
                slug="crm",
                name="CRM old",
                visibility="organization",
                material=_mcp_material(),
            )
        )
        await service.revoke_mcp(org_id=1, mcp_id=first.asset_id)
        monkeypatch.setattr(
            "app.services.asset_catalog.settings.MCP_RUNTIME_IDENTITY_MODE",
            "canary",
        )

        with pytest.raises(AssetCatalogError) as error:
            await service.create_mcp(
                CreateMcpAssetCommand(
                    org_id=1,
                    owner_user_id=9,
                    slug="crm",
                    name="CRM new",
                    visibility="organization",
                    material=_mcp_material(),
                )
            )

        assert error.value.code == "MCP_SLUG_EXISTS"
    await engine.dispose()


@pytest.mark.asyncio
async def test_archive_and_revoke_preserve_immutable_versions() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        service = AssetCatalogService(session)
        skill = await service.create_skill(_create_skill())
        mcp = await service.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=2,
                slug="crm",
                name="CRM",
                visibility="organization",
                material=_mcp_material(),
            )
        )
        await service.archive_skill(org_id=1, skill_id=skill.asset_id)
        await service.revoke_mcp(org_id=1, mcp_id=mcp.asset_id)
        await session.commit()

        skill_row = await session.get(Skill, skill.asset_id)
        mcp_row = await session.get(Mcp, mcp.asset_id)
        assert skill_row is not None and skill_row.status == "archived"
        assert mcp_row is not None and mcp_row.status == "revoked"
        assert await session.scalar(select(func.count(SkillVersion.id))) == 1
        assert await session.scalar(select(func.count(McpVersion.id))) == 1
    await engine.dispose()
