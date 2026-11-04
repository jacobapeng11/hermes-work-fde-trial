from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetRolloutBatch, AssetRolloutTarget, Skill
from app.schemas.asset_rollout import AssetRolloutCommand
from app.schemas.profile_desired import ProfileDesiredResult
from app.services.asset_rollout import AssetRolloutService
from app.services.asset_catalog import AssetCatalogError, AssetCatalogService


@dataclass(frozen=True)
class PersonalSkillRemovalResult:
    skill_id: int
    rollout_id: str
    affected_profile_ids: tuple[int, ...]
    desired_results: tuple[ProfileDesiredResult, ...]
    storage_slug: str


class PersonalSkillLifecycleService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def archive(
        self,
        *,
        org_id: int,
        user_id: int,
        skill_id: int,
    ) -> None:
        skill = await self._owned_personal_skill(
            org_id=org_id,
            user_id=user_id,
            skill_id=skill_id,
            include_deleted=True,
        )
        await AssetCatalogService(self.db).archive_skill(
            org_id=org_id,
            skill_id=int(skill.id),
        )

    async def delete(
        self,
        *,
        org_id: int,
        user_id: int,
        skill_id: int,
        operation_id: str,
    ) -> PersonalSkillRemovalResult:
        skill = await self._owned_personal_skill(
            org_id=org_id,
            user_id=user_id,
            skill_id=skill_id,
        )
        rollout_id = f"personal-skill-delete-{int(skill.id)}"
        await AssetRolloutService(self.db).create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=org_id,
                asset_type="skill",
                asset_id=int(skill.id),
                action="delete",
                source_version_id=skill.current_version_id,
                created_by_user_id=user_id,
                change_summary="Personal Skill permanently deleted by its owner",
            )
        )
        profile_ids = tuple(
            int(item)
            for item in await self.db.scalars(
                select(AssetRolloutTarget.runtime_agent_profile_id)
                .join(
                    AssetRolloutBatch,
                    AssetRolloutBatch.id
                    == AssetRolloutTarget.asset_rollout_batch_id,
                )
                .where(AssetRolloutBatch.rollout_id == rollout_id)
                .order_by(AssetRolloutTarget.runtime_agent_profile_id)
            )
        )
        storage_slug = self._base_slug(str(skill.slug), int(skill.id))
        if str(skill.status) != "deleted":
            skill.status = "deleted"
            skill.archived_at = skill.archived_at or datetime.utcnow()
            skill.revoked_at = skill.revoked_at or datetime.utcnow()
            skill.slug = self._deleted_slug(str(skill.slug), int(skill.id))
        await self.db.flush()
        return PersonalSkillRemovalResult(
            skill_id=int(skill.id),
            rollout_id=rollout_id,
            affected_profile_ids=profile_ids,
            desired_results=(),
            storage_slug=storage_slug,
        )

    async def _owned_personal_skill(
        self,
        *,
        org_id: int,
        user_id: int,
        skill_id: int,
        include_deleted: bool = False,
    ) -> Skill:
        statement = select(Skill).where(
                Skill.id == skill_id,
                Skill.org_id == org_id,
                Skill.owner_user_id == user_id,
                Skill.asset_scope == "personal",
            )
        if not include_deleted:
            statement = statement.where(Skill.status != "deleted")
        skill = await self.db.scalar(statement.with_for_update())
        if skill is None:
            raise AssetCatalogError(
                "PERSONAL_SKILL_UNAVAILABLE",
                "Personal Skill is unavailable",
            )
        return skill

    @staticmethod
    def _deleted_slug(slug: str, skill_id: int) -> str:
        suffix = f"--deleted-{skill_id}"
        return f"{slug[: max(1, 128 - len(suffix))]}{suffix}"

    @staticmethod
    def _base_slug(slug: str, skill_id: int) -> str:
        suffix = f"--deleted-{skill_id}"
        return slug.removesuffix(suffix)
