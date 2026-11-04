from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    InstalledExpertBinding,
    ProfileConvergenceJob,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    Skill,
    SkillVersion,
)
from app.schemas.experts import (
    ApproveExpertReviewRequest,
    CreateExpertRequest,
    ExpertCapabilitySelection,
    ExpertShareScope,
)
from tests.unit.expert_service_fixture import ExpertServiceFixture as ExpertProfileOrchestrator
from app.services.runtime_workspace import Principal
from tests.unit.test_expert_profiles import _database


@pytest.mark.asyncio
async def test_private_expert_create_and_config_use_only_profile_desired() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            skill = Skill(
                org_id=1,
                owner_user_id=1,
                slug="stable-search",
                name="Stable Search",
                category_key="productivity",
                asset_scope="public",
                visibility="organization",
                status="published",
            )
            db.add(skill)
            await db.flush()
            version = SkillVersion(
                org_id=1,
                skill_id=int(skill.id),
                version=1,
                package_uri="/asset/stable-search",
                package_sha256="a" * 64,
                manifest={"entrypoint": "SKILL.md"},
                status="published",
            )
            db.add(version)
            await db.flush()
            skill.current_version_id = int(version.id)
            await db.commit()

            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="Desired 专家",
                    soul_content="只写 Desired",
                ),
            )
            assert created.status == "active"
            profile = await db.get(RuntimeAgentProfile, created.runtime_agent_profile_id)
            assert profile is not None
            assert profile.desired_revision == 1
            assert profile.applied_revision == 0

            configured = await service.replace_capabilities(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=int(profile.id),
                capabilities=ExpertCapabilitySelection(
                    skills=[{"source": "public", "asset_id": int(skill.id)}]
                ),
            )
            assert configured.status == "active"
            assert configured.convergence_status == "waiting"
            assert profile.desired_revision == 2
            assert await db.scalar(
                select(func.count(ProfileSkillBinding.id)).where(
                    ProfileSkillBinding.runtime_agent_profile_id == profile.id,
                    ProfileSkillBinding.skill_id == skill.id,
                )
            ) == 1
            assert await db.scalar(
                select(func.count(RuntimeAgentProfileRevision.id)).where(
                    RuntimeAgentProfileRevision.runtime_agent_profile_id == profile.id
                )
            ) == 2
            job = await db.scalar(
                select(ProfileConvergenceJob).where(
                    ProfileConvergenceJob.runtime_agent_profile_id == profile.id
                )
            )
            assert job is not None and job.target_revision == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_market_first_install_creates_binding_and_desired_without_dashboard() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            owner = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="市场 Desired 专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            assert owner.review_id is not None
            await service.approve_review(
                org_id=1,
                reviewer_user_id=1,
                review_id=int(owner.review_id),
                command=ApproveExpertReviewRequest(),
            )

            installed = await service.install_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
                expert_package_id=int(owner.expert_package_id),
            )
            profile = await db.get(
                RuntimeAgentProfile,
                installed.runtime_agent_profile_id,
            )
            binding = await db.scalar(
                select(InstalledExpertBinding).where(
                    InstalledExpertBinding.runtime_agent_profile_id == profile.id
                )
            )
            assert profile is not None
            assert profile.desired_revision == 1
            assert profile.applied_revision == 0
            assert profile.status == "active"
            assert binding is not None
            assert binding.status == "enabled"
            assert binding.projection_config == {"schema_version": "profile-desired/v1"}
    finally:
        await engine.dispose()
