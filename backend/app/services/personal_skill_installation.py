"""Atomic upload-and-install command for the Skill page."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.profile_desired import (
    DefaultProfileAssetMutation,
    ProfileDesiredResult,
)
from app.services.personal_skill_assets import (
    PersonalSkillAssetRecord,
    PersonalSkillAssetService,
    PersonalSkillUploadResult,
    SqlAlchemyPersonalSkillAssetStore,
)
from app.services.plaza.user_asset_scope import DefaultUserAssetScope
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.runtime_workspace import Principal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersonalSkillInstallResult:
    asset: PersonalSkillAssetRecord
    operation_id: str
    desired: ProfileDesiredResult


class PersonalSkillInstallationService:
    """Commit asset/version/binding/Desired together and compensate files."""

    def __init__(self, db: AsyncSession, *, root: str) -> None:
        self.db = db
        self.assets = PersonalSkillAssetService(
            store=SqlAlchemyPersonalSkillAssetStore(db),
            root=root,
        )

    async def upload_and_install(
        self,
        *,
        principal: Principal,
        scope: DefaultUserAssetScope,
        content: bytes,
        category_key: str = "uncategorized",
        description: str | None = None,
        operation_id: str | None = None,
    ) -> PersonalSkillInstallResult:
        upload: PersonalSkillUploadResult | None = None
        command_id = operation_id or f"personal-skill-install-{uuid4().hex}"
        try:
            upload = await self.assets.upload_skill_zip_with_artifact(
                principal=principal,
                content=content,
                category_key=category_key,
                description=description,
            )
            desired = await ProfileDesiredMutationService(
                self.db
            ).mutate_default_asset(
                DefaultProfileAssetMutation(
                    org_id=principal.org_id,
                    user_id=principal.user_id,
                    runtime_workspace_id=scope.runtime_workspace_id,
                    runtime_agent_profile_id=scope.runtime_agent_profile_id,
                    asset_type="skill",
                    action="install",
                    asset_id=upload.record.skill_id,
                    operation_id=command_id,
                )
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            if upload is not None:
                try:
                    self.assets.discard_upload(upload)
                except Exception:
                    logger.exception(
                        "personal Skill file compensation failed operation_id=%s",
                        command_id,
                    )
            raise
        return PersonalSkillInstallResult(
            asset=upload.record,
            operation_id=command_id,
            desired=desired,
        )


__all__ = ["PersonalSkillInstallResult", "PersonalSkillInstallationService"]
