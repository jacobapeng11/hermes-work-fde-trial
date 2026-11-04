from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    ProfileSkillBinding,
    Skill,
    SkillVersion,
)
from app.services.expert_rollout import ExpertRolloutService
from app.services.legacy_expert_asset_migration import (
    LegacyExpertAssetMigrationError,
    LegacyExpertAssetMigrationService,
)
from app.services.plaza.public_catalog.package_parser import sha256_directory
from app.services.profile_desired import ProfileDesiredService
from tests.unit.test_profile_desired import _database, _profile


def _artifact(tmp_path, *, artifact_id: int, version_id: int, content: str):
    path = tmp_path / f"artifact-{artifact_id}"
    path.mkdir()
    (path / "SKILL.md").write_text(content, encoding="utf-8")
    return ExpertVersionArtifact(
        id=artifact_id,
        org_id=1,
        expert_package_version_id=version_id,
        artifact_type="skill_package",
        source_type="default_installed",
        source_asset_id=591,
        name="Legacy Skill",
        storage_uri=str(path),
        sha256=sha256_directory(path),
        status="ready",
    )


@pytest.mark.asyncio
async def test_each_expert_version_keeps_stable_skill_with_exact_audit_version(tmp_path) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        package = ExpertPackage(
            id=70,
            org_id=1,
            owner_user_id=2,
            slug="versioned-expert",
            name="Versioned Expert",
            status="published",
            visibility="organization",
            current_version_id=81,
        )
        versions = [
            ExpertPackageVersion(
                id=version_id,
                org_id=1,
                expert_package_id=70,
                version=number,
                manifest={},
                skill_refs=[{"source": "default_installed", "asset_id": 591}],
                status="published",
            )
            for version_id, number in ((80, 1), (81, 2))
        ]
        artifacts = [
            _artifact(
                tmp_path,
                artifact_id=90,
                version_id=80,
                content="---\nname: legacy-v1\n---\n",
            ),
            _artifact(
                tmp_path,
                artifact_id=91,
                version_id=81,
                content="---\nname: legacy-v2\n---\n",
            ),
        ]
        session.add_all([package, *versions, *artifacts])
        await session.commit()

        service = LegacyExpertAssetMigrationService(session)
        first = await service.migrate(
            skill_map={},
            skill_version_map={},
            mcp_map={},
            installed_skill_map={},
        )
        await session.flush()
        second = await service.migrate(
            skill_map={},
            skill_version_map={},
            mcp_map={},
            installed_skill_map={},
        )

        assert first == 2
        assert second == 0
        assert versions[0].skill_refs[0]["asset_id"] == versions[1].skill_refs[0]["asset_id"]
        assert versions[0].skill_refs[0]["version_id"] != versions[1].skill_refs[0]["version_id"]
        source = await session.get(Skill, versions[0].skill_refs[0]["asset_id"])
        assert source is not None and source.status == "archived"
        source_versions = (
            await session.scalars(
                select(SkillVersion)
                .where(SkillVersion.skill_id == source.id)
                .order_by(SkillVersion.version.asc())
            )
        ).all()
        assert len(source_versions) == 2
        assert [item.package_sha256 for item in source_versions] == [
            artifacts[0].sha256,
            artifacts[1].sha256,
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_market_expert_desired_follows_migrated_stable_skill(tmp_path) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        package = ExpertPackage(
            id=70,
            org_id=1,
            owner_user_id=2,
            slug="installable-expert",
            name="Installable Expert",
            status="published",
            visibility="organization",
            current_version_id=80,
        )
        version = ExpertPackageVersion(
            id=80,
            org_id=1,
            expert_package_id=70,
            version=1,
            manifest={"basic_info": {"name": "Installable Expert"}},
            skill_refs=[{"source": "default_installed", "asset_id": 591}],
            status="published",
        )
        artifact = _artifact(
            tmp_path,
            artifact_id=90,
            version_id=80,
            content="---\nname: installable\n---\n",
        )
        profile = _profile(
            profile_id=11,
            expert_package_id=70,
            ownership_type="market_installed",
        )
        session.add_all([package, version, artifact, profile])
        await session.commit()

        await LegacyExpertAssetMigrationService(session).migrate(
            skill_map={},
            skill_version_map={},
            mcp_map={},
            installed_skill_map={},
        )
        command = await ExpertRolloutService(session).build_desired_command(
            profile=profile,
            version=version,
            source_id="migration-test",
            created_by_user_id=2,
                source_type="migration",
        )
        result = await ProfileDesiredService(session).replace(command)

        binding = await session.scalar(select(ProfileSkillBinding))
        assert result.created_revision is True
        assert binding is not None and binding.version_policy == "follow_latest"
        resolved = await session.get(SkillVersion, binding.skill_version_id)
        assert resolved is not None
        assert resolved.skill_id == binding.skill_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_unrecoverable_asset_blocks_installable_market_head() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        package = ExpertPackage(
            id=70,
            org_id=1,
            owner_user_id=2,
            slug="broken-market-expert",
            name="Broken Market Expert",
            status="published",
            visibility="organization",
            current_version_id=80,
        )
        version = ExpertPackageVersion(
            id=80,
            org_id=1,
            expert_package_id=70,
            version=1,
            manifest={},
            mcp_refs=[{"mcp_id": 11}],
            status="published",
        )
        session.add_all([package, version])
        await session.commit()

        with pytest.raises(LegacyExpertAssetMigrationError) as error:
            await LegacyExpertAssetMigrationService(session).migrate(
                skill_map={},
                skill_version_map={},
                mcp_map={},
                installed_skill_map={},
            )

        assert error.value.code == "LEGACY_EXPERT_REQUIRED_ASSET_UNRECOVERABLE"
        assert await session.scalar(select(func.count(Skill.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_unrecoverable_withdrawn_history_is_audited_not_guessed() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        package = ExpertPackage(
            id=70,
            org_id=1,
            owner_user_id=2,
            slug="withdrawn-expert",
            name="Withdrawn Expert",
            status="withdrawn",
            visibility="organization",
            current_version_id=80,
        )
        version = ExpertPackageVersion(
            id=80,
            org_id=1,
            expert_package_id=70,
            version=1,
            manifest={},
            mcp_refs=[{"mcp_id": 11}],
            status="published",
        )
        session.add_all([package, version])
        await session.commit()

        changed = await LegacyExpertAssetMigrationService(session).migrate(
            skill_map={},
            skill_version_map={},
            mcp_map={},
            installed_skill_map={},
        )

        assert changed == 1
        assert version.mcp_refs == []
        assert version.manifest["asset_migration"]["status"] == "partial"
        assert version.manifest["asset_migration"]["unresolved_refs"] == [
            {
                "asset_type": "mcp",
                "source": "public",
                "legacy_asset_id": 11,
                "legacy_version_id": None,
                "reason": "immutable_artifact_missing",
            }
        ]
    await engine.dispose()
