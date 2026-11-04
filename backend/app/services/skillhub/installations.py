from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Skill, UserSkillHubInstallation
from app.schemas.asset_catalog import (
    CreateSkillAssetCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.schemas.profile_desired import DefaultProfileAssetMutation
from app.services.asset_catalog import AssetCatalogService
from app.services.plaza.public_catalog.package_parser import sha256_directory
from app.services.plaza.user_skill_operations.operation_queue import (
    OperationSnapshot,
    UserSkillOperationQueue,
)
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.skillhub.client import SkillHubClient
from app.services.skillhub.market import SkillHubMarketService, skillhub_target
from app.services.skillhub.package_storage import UserSkillHubPackageStorage


class SkillHubInstallationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SkillHubInstallationOperationService:
    """Queue SkillHub network work, then write only stable catalog and Desired state."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        queue: UserSkillOperationQueue,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue

    async def submit_install(
        self,
        *,
        org_id: int,
        user_id: int,
        namespace: str,
        public_slug: str,
        expected_version: str,
        workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> OperationSnapshot:
        target = skillhub_target(namespace, public_slug)

        async def handler(snapshot: OperationSnapshot) -> None:
            await self._install(
                snapshot=snapshot,
                org_id=org_id,
                user_id=user_id,
                namespace=namespace,
                public_slug=public_slug,
                expected_version=expected_version,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )

        return await self.queue.submit(
            workspace_id=workspace_id,
            action="install",
            target_key=target,
            handler=handler,
            org_id=org_id,
            user_id=user_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )

    async def submit_sync(
        self,
        *,
        org_id: int,
        user_id: int,
        skill_id: int,
        workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> OperationSnapshot:
        async with self.session_factory() as db:
            installation = await self._owned_installation(
                db,
                org_id=org_id,
                user_id=user_id,
                skill_id=skill_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )
            if (
                installation.source_status != "update_available"
                or not installation.available_upstream_version
            ):
                raise SkillHubInstallationError("update_not_available")
            namespace = str(installation.namespace)
            public_slug = str(installation.public_slug)
            expected_version = str(installation.available_upstream_version)
        target = skillhub_target(namespace, public_slug)

        async def handler(snapshot: OperationSnapshot) -> None:
            await self._install(
                snapshot=snapshot,
                org_id=org_id,
                user_id=user_id,
                namespace=namespace,
                public_slug=public_slug,
                expected_version=expected_version,
                runtime_agent_profile_id=runtime_agent_profile_id,
                existing_skill_id=skill_id,
            )

        return await self.queue.submit(
            workspace_id=workspace_id,
            action="sync",
            target_key=target,
            handler=handler,
            org_id=org_id,
            user_id=user_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )

    async def submit_uninstall(
        self,
        *,
        org_id: int,
        user_id: int,
        skill_id: int,
        workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> OperationSnapshot:
        async with self.session_factory() as db:
            installation = await self._owned_installation(
                db,
                org_id=org_id,
                user_id=user_id,
                skill_id=skill_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )
            target = skillhub_target(str(installation.namespace), str(installation.public_slug))

        async def handler(snapshot: OperationSnapshot) -> None:
            async with self.session_factory() as db:
                installation = await self._owned_installation(
                    db,
                    org_id=org_id,
                    user_id=user_id,
                    skill_id=skill_id,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    lock=True,
                )
                await ProfileDesiredMutationService(db).mutate_default_asset(
                    DefaultProfileAssetMutation(
                        operation_id=snapshot.operation_id,
                        org_id=org_id,
                        user_id=user_id,
                        runtime_workspace_id=workspace_id,
                        runtime_agent_profile_id=runtime_agent_profile_id,
                        asset_type="skill",
                        asset_id=skill_id,
                        action="uninstall",
                    )
                )
                await db.delete(installation)
                await db.commit()

        return await self.queue.submit(
            workspace_id=workspace_id,
            action="uninstall",
            target_key=target,
            handler=handler,
            org_id=org_id,
            user_id=user_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )

    async def _install(
        self,
        *,
        snapshot: OperationSnapshot,
        org_id: int,
        user_id: int,
        namespace: str,
        public_slug: str,
        expected_version: str,
        runtime_agent_profile_id: int,
        existing_skill_id: int | None = None,
    ) -> None:
        async with self.session_factory() as lookup_db:
            market = SkillHubMarketService(
                session=lookup_db,
                operation_queue=self.queue,
            )
            remote = await market.resolve_eligible(
                namespace=namespace,
                public_slug=public_slug,
                expected_version=expected_version,
            )
        verified = await SkillHubClient().download_and_verify(skill=remote.skill)
        stored = UserSkillHubPackageStorage().store(
            asset_root=(
                Path(settings.HERMES_PUBLIC_ASSET_ROOT) / "skillhub" / str(org_id) / str(user_id)
            ),
            namespace=namespace,
            public_slug=public_slug,
            version=remote.skill.version,
            content=verified.content,
        )
        manifest = {
            "kind": "skill",
            "entrypoint": "SKILL.md",
            "skill_metadata": stored.metadata,
            "skillhub": {
                "namespace": namespace,
                "public_slug": public_slug,
                "upstream_version": remote.skill.version,
            },
        }
        material = SkillVersionMaterial(
            package_uri=stored.deployment_uri,
            package_sha256=sha256_directory(Path(stored.deployment_uri)),
            manifest=manifest,
        )
        async with self.session_factory() as db:
            catalog = AssetCatalogService(db)
            installation = await db.scalar(
                select(UserSkillHubInstallation)
                .where(
                    UserSkillHubInstallation.org_id == org_id,
                    UserSkillHubInstallation.user_id == user_id,
                    UserSkillHubInstallation.runtime_agent_profile_id == runtime_agent_profile_id,
                    UserSkillHubInstallation.namespace == namespace,
                    UserSkillHubInstallation.public_slug == public_slug,
                )
                .with_for_update()
            )
            if existing_skill_id is None and installation is not None:
                raise SkillHubInstallationError("skill_already_installed")
            if existing_skill_id is not None:
                if installation is None or int(installation.skill_id) != existing_skill_id:
                    raise SkillHubInstallationError("skillhub_installation_missing")
                result = await catalog.publish_skill_version(
                    PublishSkillVersionCommand(
                        org_id=org_id,
                        skill_id=existing_skill_id,
                        material=material,
                    )
                )
                skill = await db.get(Skill, existing_skill_id)
                assert skill is not None
            else:
                slug = self._stable_slug(namespace, public_slug)
                skill = await db.scalar(
                    select(Skill).where(
                        Skill.org_id == org_id,
                        Skill.owner_user_id == user_id,
                        Skill.slug == slug,
                        Skill.asset_scope == "personal",
                        Skill.status != "deleted",
                    )
                )
                if skill is None:
                    result = await catalog.create_skill(
                        CreateSkillAssetCommand(
                            org_id=org_id,
                            owner_user_id=user_id,
                            slug=slug,
                            name=remote.skill.name,
                            description=remote.skill.description,
                            asset_scope="personal",
                            visibility="private",
                            material=material,
                        )
                    )
                    skill = await db.get(Skill, result.asset_id)
                    assert skill is not None
                else:
                    result = await catalog.publish_skill_version(
                        PublishSkillVersionCommand(
                            org_id=org_id,
                            skill_id=int(skill.id),
                            material=material,
                        )
                    )
            skill.category_key = remote.category_key
            now = datetime.utcnow()
            if installation is None:
                installation = UserSkillHubInstallation(
                    org_id=org_id,
                    user_id=user_id,
                    runtime_workspace_id=snapshot.workspace_id,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    skill_id=int(skill.id),
                    installed_skill_version_id=result.version_id,
                    source_status="current",
                    namespace=namespace,
                    public_slug=public_slug,
                    upstream_internal_slug=remote.skill.upstream_internal_slug,
                    installed_upstream_version=remote.skill.version,
                    upstream_category_snapshot=remote.skill.category,
                    upstream_name_snapshot=remote.skill.name,
                    upstream_description_snapshot=remote.skill.description,
                    package_sha256=verified.package_sha256,
                    signature_key_id=verified.signature_key_id,
                    signature_verified_at=now,
                    verified_content_sha256=verified.verified_content_sha256,
                    last_version_checked_at=now,
                    package_uri=stored.package_uri,
                )
                db.add(installation)
            else:
                installation.installed_skill_version_id = result.version_id
                installation.source_status = "current"
                installation.installed_upstream_version = remote.skill.version
                installation.available_upstream_version = None
                installation.upstream_category_snapshot = remote.skill.category
                installation.upstream_name_snapshot = remote.skill.name
                installation.upstream_description_snapshot = remote.skill.description
                installation.package_sha256 = verified.package_sha256
                installation.signature_key_id = verified.signature_key_id
                installation.signature_verified_at = now
                installation.verified_content_sha256 = verified.verified_content_sha256
                installation.last_version_checked_at = now
                installation.last_check_error = None
                installation.package_uri = stored.package_uri
            await ProfileDesiredMutationService(db).mutate_default_asset(
                DefaultProfileAssetMutation(
                    operation_id=snapshot.operation_id,
                    org_id=org_id,
                    user_id=user_id,
                    runtime_workspace_id=snapshot.workspace_id,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                    asset_type="skill",
                    asset_id=int(skill.id),
                    action="install",
                )
            )
            await db.commit()

    @staticmethod
    def _stable_slug(namespace: str, public_slug: str) -> str:
        digest = hashlib.sha256(f"{namespace}:{public_slug}".encode()).hexdigest()[:24]
        return f"skillhub-{digest}"

    @staticmethod
    async def _owned_installation(
        db: AsyncSession,
        *,
        org_id: int,
        user_id: int,
        skill_id: int,
        runtime_agent_profile_id: int,
        lock: bool = False,
    ) -> UserSkillHubInstallation:
        statement = select(UserSkillHubInstallation).where(
            UserSkillHubInstallation.org_id == org_id,
            UserSkillHubInstallation.user_id == user_id,
            UserSkillHubInstallation.runtime_agent_profile_id == runtime_agent_profile_id,
            UserSkillHubInstallation.skill_id == skill_id,
        )
        if lock:
            statement = statement.with_for_update()
        installation = await db.scalar(statement)
        if installation is None:
            raise SkillHubInstallationError("skillhub_installation_missing")
        return installation


__all__ = ["SkillHubInstallationError", "SkillHubInstallationOperationService"]
