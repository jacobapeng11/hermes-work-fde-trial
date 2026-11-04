from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    Mcp,
    HermesRuntimeBinding,
    InstalledExpertBinding,
    Permission,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfileRevision,
    Skill,
)
from app.models.legacy_assets import (
    LegacyAssetBase,
    LegacyProfileAssetDraft as ProfileAssetDraft,
    LegacyPublicAsset as PublicAsset,
    LegacyPublicMcpAsset as PublicMcpAsset,
    LegacyPublicSkillAsset as PublicSkillAsset,
    LegacyPublicSkillVersion as PublicSkillVersion,
    LegacyUserRuntimeMcp as UserRuntimeMcp,
    LegacyUserRuntimeSkill as UserRuntimeSkill,
)
from app.services.asset_unification_migration import (
    AssetUnificationMigrationError,
    AssetUnificationMigrationService,
)
from app.services.plaza.public_catalog.package_parser import sha256_directory
from app.config import settings
from tests.unit.test_profile_desired import _database, _profile


def _legacy_skill() -> tuple[PublicSkillAsset, PublicSkillVersion]:
    asset = PublicSkillAsset(
        id=71,
        org_id=1,
        normalized_name="legacy-search",
        active_name_key="legacy-search",
        name="Legacy Search",
        category_key="productivity",
        status="active",
        current_version_id=81,
        created_by_user_id=2,
    )
    version = PublicSkillVersion(
        id=81,
        org_id=1,
        public_skill_asset_id=71,
        version_number=1,
        platform_version="v1",
        original_package_uri="/asset/legacy-search-v1.zip",
        deployment_package_uri="/asset/legacy-search-v1",
        original_sha256="1" * 64,
        deployment_sha256="2" * 64,
        skill_metadata={"name": "legacy-search"},
        manifest={"entrypoint": "SKILL.md"},
        created_by_user_id=2,
        status="published",
    )
    return asset, version


def _legacy_installation() -> UserRuntimeSkill:
    now = datetime.utcnow()
    return UserRuntimeSkill(
        id=91,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        runtime_agent_profile_id=10,
        source="market",
        public_skill_asset_id=71,
        installed_public_skill_version_id=81,
        update_status="current",
        version_label="v1",
        normalized_display_name="legacy-search",
        name_snapshot="Legacy Search",
        category_key_snapshot="productivity",
        runtime_category_key="productivity",
        runtime_directory_key="market-71",
        runtime_relative_path="productivity/market-71",
        runtime_skill_name="market-71-stable",
        installed_package_sha256="2" * 64,
        installed_at=now,
        last_updated_at=now,
    )


def _legacy_mcp() -> PublicMcpAsset:
    return PublicMcpAsset(
        id=72,
        org_id=1,
        slug="legacy-crm",
        active_slug_key="legacy-crm",
        name="Legacy CRM",
        transport="streamable-http",
        runtime_config={
            "name": "Legacy CRM",
            "transport": "streamable-http",
            "enabled": True,
            "url": "https://mcp.invalid/mcp",
        },
        status="active",
        created_by_user_id=2,
    )


@pytest.mark.asyncio
async def test_migration_unifies_ids_builds_desired_and_is_idempotent() -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    async with sessions() as session:
        skill, version = _legacy_skill()
        session.add_all(
            [
                _profile(),
                skill,
                version,
                _legacy_installation(),
                _legacy_mcp(),
                HermesRuntimeBinding(
                    id=119,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_id="rt-119",
                    image="hermes:test",
                    k8s_namespace="test",
                    knative_service="rt-119",
                    endpoint_base_url="http://runtime.invalid",
                    endpoint_host_header="runtime.invalid",
                    hermes_home_sub_path="users/2",
                    workspace_sub_path="workspaces/3",
                    hermes_home_mount_path="/home/hermes",
                    workspace_mount_path="/workspace",
                    api_key_encrypted="secret",
                    status="ready",
                ),
                UserRuntimeMcp(
                    id=92,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_agent_profile_id=10,
                    public_mcp_asset_id=72,
                    installed_at=datetime.utcnow(),
                ),
            ]
        )
        await session.commit()

        first = await AssetUnificationMigrationService(session).migrate()
        await session.commit()
        second = await AssetUnificationMigrationService(session).migrate()
        await session.commit()

        stable_skill = await session.scalar(
            select(Skill).where(Skill.slug == "legacy-public-skill-71")
        )
        stable_mcp = await session.scalar(select(Mcp).where(Mcp.slug == "legacy-crm"))
        assert stable_skill is not None and stable_mcp is not None
        assert first.public_skill_assets == 1
        assert first.public_skill_versions == 1
        assert first.public_mcp_assets == 1
        assert first.migrated_profiles == 1
        assert second.migrated_profiles == 0
        assert await session.scalar(select(func.count(RuntimeAgentProfileRevision.id))) == 1
        assert (
            await session.scalar(
                select(func.count(ProfileSkillBinding.id)).where(
                    ProfileSkillBinding.skill_id == stable_skill.id
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(ProfileMcpBinding.id)).where(
                    ProfileMcpBinding.mcp_id == stable_mcp.id
                )
            )
            == 1
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_cutover_normalizes_lifecycle_permission_policy_and_install_epoch() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        profile = _profile()
        profile.status = "ready"
        session.add_all(
            [
                profile,
                Permission(
                    org_id=1,
                    asset_type="expert_package",
                    asset_id=501,
                    grantee_type="organization",
                    grantee_id="root",
                    kb_role="export",
                ),
                Permission(
                    org_id=1,
                    asset_type="expert_package",
                    asset_id=501,
                    grantee_type="user",
                    grantee_id="9",
                    kb_role="preview",
                ),
                ProfileSkillBinding(
                    org_id=1,
                    runtime_workspace_id=3,
                    runtime_agent_profile_id=10,
                    skill_id=71,
                    skill_version_id=81,
                    version_policy="pinned",
                    status="enabled",
                ),
                InstalledExpertBinding(
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    expert_package_id=501,
                    expert_package_version_id=601,
                    runtime_agent_profile_id=10,
                    source_type="market",
                    status="enabled",
                    installation_epoch=0,
                ),
            ]
        )
        await session.commit()
        service = AssetUnificationMigrationService(session)

        assert await service._normalize_profile_lifecycles() == 1
        assert await service._normalize_expert_permissions() > 0
        assert await service._normalize_stable_skill_bindings() == 1
        assert await service._normalize_installation_epochs() == 1
        await session.commit()

        permission = await session.scalar(
            select(Permission).where(Permission.asset_id == 501)
        )
        binding = await session.scalar(select(ProfileSkillBinding))
        installation = await session.scalar(select(InstalledExpertBinding))
        assert profile.status == "active"
        assert permission is not None
        assert (permission.grantee_type, permission.grantee_id, permission.kb_role) == (
            "all",
            "*",
            None,
        )
        assert await session.scalar(
            select(func.count(Permission.id)).where(Permission.asset_id == 501)
        ) == 1
        assert binding is not None
        assert binding.version_policy == "follow_latest"
        assert binding.skill_version_id is None
        assert installation is not None and installation.installation_epoch == 1

        assert await service._normalize_profile_lifecycles() == 0
        assert await service._normalize_expert_permissions() == 0
        assert await service._normalize_stable_skill_bindings() == 0
        assert await service._normalize_installation_epochs() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_blocks_unsaved_draft_before_writing() -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                _profile(),
                ProfileAssetDraft(
                    id=1,
                    profile_id=3,
                    asset_type="skill",
                    asset_id=71,
                    source="public_asset",
                    action="install",
                    status="staged",
                ),
            ]
        )
        await session.commit()

        with pytest.raises(AssetUnificationMigrationError) as error:
            await AssetUnificationMigrationService(session).migrate()

        assert error.value.code == "UNSAVED_PROFILE_DRAFT"
        assert await session.scalar(select(func.count(Skill.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_does_not_create_desired_for_deleted_profile() -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    async with sessions() as session:
        profile = _profile()
        profile.status = "deleted"
        session.add(profile)
        await session.commit()

        result = await AssetUnificationMigrationService(session).migrate()

        assert result.migrated_profiles == 0
        assert await session.scalar(select(func.count(RuntimeAgentProfileRevision.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_skips_deleted_public_skill_without_versions() -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                _profile(),
                PublicSkillAsset(
                    id=71,
                    org_id=1,
                    normalized_name="deleted-empty-skill",
                    name="Deleted Empty Skill",
                    category_key="other",
                    status="deleted",
                    current_version_id=None,
                    created_by_user_id=2,
                    deleted_at=datetime.utcnow(),
                ),
            ]
        )
        await session.commit()

        result = await AssetUnificationMigrationService(session).migrate()

        assert result.public_skill_assets == 0
        assert await session.scalar(select(func.count(Skill.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_blocks_active_public_skill_without_versions() -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                _profile(),
                PublicSkillAsset(
                    id=71,
                    org_id=1,
                    normalized_name="active-empty-skill",
                    name="Active Empty Skill",
                    category_key="other",
                    status="active",
                    current_version_id=None,
                    created_by_user_id=2,
                ),
            ]
        )
        await session.commit()

        with pytest.raises(AssetUnificationMigrationError) as error:
            await AssetUnificationMigrationService(session).migrate()

        assert error.value.code == "LEGACY_SKILL_VERSION_MISSING"
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_unifies_earliest_generic_public_mcp_directory() -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    async with sessions() as session:
        session.add_all(
            [
                _profile(),
                PublicAsset(
                    id=61,
                    org_id=1,
                    publisher_user_id=2,
                    asset_type="mcp",
                    slug="legacy-http-mcp",
                    version="v1",
                    name="Legacy HTTP MCP",
                    status="active",
                    storage_uri="/asset/mcps/legacy-http-mcp/v1/manifest.json",
                    manifest={
                        "transport": "http",
                        "endpoint": "https://mcp.invalid/mcp",
                        "config": {},
                    },
                    summary={},
                ),
            ]
        )
        await session.commit()

        first = await AssetUnificationMigrationService(session).migrate()
        await session.commit()
        second = await AssetUnificationMigrationService(session).migrate()
        await session.commit()

        stable = await session.scalar(select(Mcp).where(Mcp.slug == "legacy-http-mcp"))
        assert stable is not None
        assert stable.transport == "streamable_http"
        assert first.public_mcp_assets == 1
        assert second.public_mcp_assets == 1
    await engine.dispose()


@pytest.mark.parametrize("inventory_digest_is_legacy_marker", [False, True])
@pytest.mark.asyncio
async def test_migration_exports_personal_runtime_skill_to_stable_catalog(
    tmp_path, monkeypatch, inventory_digest_is_legacy_marker: bool
) -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    runtime_root = tmp_path / "runtime-root"
    source = runtime_root / "users/2/skills/personal/my-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: my-skill\n---\n", encoding="utf-8")
    digest = sha256_directory(source)
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path / "assets"))
    now = datetime.utcnow()
    async with sessions() as session:
        session.add_all(
            [
                _profile(),
                HermesRuntimeBinding(
                    id=119,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_id="rt-119",
                    image="hermes:test",
                    k8s_namespace="test",
                    knative_service="rt-119",
                    runtime_root_host_path=str(runtime_root),
                    hermes_home_sub_path="users/2",
                    workspace_sub_path="workspaces/3",
                    hermes_home_mount_path="/home/hermes",
                    workspace_mount_path="/workspace",
                    api_key_encrypted="secret",
                    status="ready",
                ),
                UserRuntimeSkill(
                    id=93,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_agent_profile_id=10,
                    source="personal",
                    update_status="not_applicable",
                    version_label="v1",
                    normalized_display_name="my-skill",
                    personal_name_key="my-skill",
                    name_snapshot="My Skill",
                    category_key_snapshot="personal",
                    runtime_category_key="personal",
                    runtime_directory_key="my-skill",
                    runtime_relative_path="personal/my-skill",
                    runtime_skill_name="my-skill",
                    installed_package_sha256=(
                        "baseline-v2" if inventory_digest_is_legacy_marker else digest
                    ),
                    installed_at=now,
                    last_updated_at=now,
                ),
            ]
        )
        await session.commit()

        dry_run = AssetUnificationMigrationService(session, materialize_files=False)
        await dry_run.migrate()
        await session.rollback()
        assert not (
            tmp_path / "assets/migrated-personal-skills/1/2/legacy-93/deployment"
        ).exists()

        result = await AssetUnificationMigrationService(session).migrate()
        await session.commit()

        stable = await session.scalar(select(Skill).where(Skill.slug == "legacy-personal-skill-93"))
        assert stable is not None
        assert result.personal_skill_assets == 1
        assert (
            await session.scalar(
                select(func.count(ProfileSkillBinding.id)).where(
                    ProfileSkillBinding.skill_id == stable.id
                )
            )
            == 1
        )
        assert (
            tmp_path / "assets/migrated-personal-skills/1/2/legacy-93/deployment/SKILL.md"
        ).is_file()
    await engine.dispose()


@pytest.mark.parametrize("inventory_digest", ["baseline-v2", "a" * 64])
@pytest.mark.asyncio
async def test_migration_skips_missing_legacy_baseline_inventory_skill(
    tmp_path, monkeypatch, inventory_digest: str
) -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    runtime_root = tmp_path / "runtime-root"
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path / "assets"))
    now = datetime.utcnow()
    async with sessions() as session:
        session.add_all(
            [
                _profile(),
                HermesRuntimeBinding(
                    id=119,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_id="rt-119",
                    image="hermes:test",
                    k8s_namespace="test",
                    knative_service="rt-119",
                    runtime_root_host_path=str(runtime_root),
                    hermes_home_sub_path="users/2",
                    workspace_sub_path="workspaces/3",
                    hermes_home_mount_path="/home/hermes",
                    workspace_mount_path="/workspace",
                    api_key_encrypted="secret",
                    status="ready",
                ),
                UserRuntimeSkill(
                    id=93,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_agent_profile_id=10,
                    source="personal",
                    update_status="not_applicable",
                    version_label="v1",
                    normalized_display_name="retired-bundled-skill",
                    personal_name_key="retired-bundled-skill",
                    name_snapshot="Retired Bundled Skill",
                    category_key_snapshot="hermes-core",
                    runtime_category_key="hermes-core",
                    runtime_directory_key="retired-bundled-skill",
                    runtime_relative_path="hermes-core/retired-bundled-skill",
                    runtime_skill_name="retired-bundled-skill",
                    installed_package_sha256=inventory_digest,
                    installed_at=now,
                    last_updated_at=now,
                ),
            ]
        )
        await session.commit()

        result = await AssetUnificationMigrationService(session).migrate()

        assert result.personal_skill_assets == 0
        assert await session.scalar(select(func.count(Skill.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_recovers_personal_skill_from_frozen_expert_artifact(
    tmp_path, monkeypatch
) -> None:
    engine, sessions = await _database()
    async with engine.begin() as connection:
        await connection.run_sync(LegacyAssetBase.metadata.create_all)
    runtime_root = tmp_path / "runtime-root"
    frozen = tmp_path / "frozen-skill"
    frozen.mkdir()
    (frozen / "SKILL.md").write_text("---\nname: recovered\n---\n", encoding="utf-8")
    digest = sha256_directory(frozen)
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path / "assets"))
    now = datetime.utcnow()
    async with sessions() as session:
        package = ExpertPackage(
            id=70,
            org_id=1,
            owner_user_id=2,
            slug="recovery-source",
            name="Recovery Source",
            status="published",
            visibility="public",
        )
        version = ExpertPackageVersion(
            id=80,
            org_id=1,
            expert_package_id=70,
            version=1,
            manifest={},
            status="published",
        )
        session.add_all(
            [
                _profile(),
                HermesRuntimeBinding(
                    id=119,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_id="rt-119",
                    image="hermes:test",
                    k8s_namespace="test",
                    knative_service="rt-119",
                    runtime_root_host_path=str(runtime_root),
                    hermes_home_sub_path="users/2",
                    workspace_sub_path="workspaces/3",
                    hermes_home_mount_path="/home/hermes",
                    workspace_mount_path="/workspace",
                    api_key_encrypted="secret",
                    status="ready",
                ),
                UserRuntimeSkill(
                    id=93,
                    org_id=1,
                    user_id=2,
                    runtime_workspace_id=3,
                    runtime_agent_profile_id=10,
                    source="personal",
                    update_status="not_applicable",
                    version_label="v1",
                    normalized_display_name="recovered",
                    personal_name_key="recovered",
                    name_snapshot="Recovered",
                    category_key_snapshot="personal",
                    runtime_category_key="personal",
                    runtime_directory_key="personal-recovered",
                    runtime_relative_path="personal/personal-recovered",
                    runtime_skill_name="recovered",
                    installed_package_sha256=digest,
                    installed_at=now,
                    last_updated_at=now,
                ),
                package,
                version,
                ExpertVersionArtifact(
                    id=90,
                    org_id=1,
                    expert_package_version_id=80,
                    artifact_type="skill_package",
                    source_type="default_installed",
                    source_asset_id=93,
                    name="Recovered",
                    storage_uri=str(frozen),
                    sha256=digest,
                    status="ready",
                ),
            ]
        )
        await session.commit()

        result = await AssetUnificationMigrationService(session).migrate()

        assert result.personal_skill_assets == 1
        assert (
            tmp_path / "assets/migrated-personal-skills/1/2/legacy-93/deployment/SKILL.md"
        ).is_file()
    await engine.dispose()
