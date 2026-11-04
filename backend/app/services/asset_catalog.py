from __future__ import annotations

from datetime import datetime
import hashlib
import json

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.hermes_runtime.skill_visibility_policy import is_platform_default_skill_name
from app.models import Mcp, McpSlugLease, McpVersion, Skill, SkillVersion
from app.schemas.asset_catalog import (
    AssetCatalogResult,
    CreateMcpAssetCommand,
    CreateSkillAssetCommand,
    McpVersionMaterial,
    PublishMcpVersionCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.services.mcp_identity import mcp_slug_namespace


class AssetCatalogError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _deleted_skill_slug(slug: str, skill_id: int) -> str:
    suffix = f"__deleted_{skill_id}"
    return f"{slug[: max(1, 128 - len(suffix))]}{suffix}"


class AssetCatalogService:
    """Create immutable asset versions without performing storage or Runtime I/O."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_skill(
        self,
        command: CreateSkillAssetCommand,
    ) -> AssetCatalogResult:
        if command.asset_scope in {"public", "personal"} and (
            is_platform_default_skill_name(command.slug)
            or is_platform_default_skill_name(command.name)
        ):
            raise AssetCatalogError(
                "SKILL_SYSTEM_RESERVED",
                "Skill name is reserved for a Runtime system Skill",
            )
        clauses = [
            Skill.org_id == command.org_id,
            Skill.slug == command.slug,
            Skill.status != "deleted",
        ]
        if command.asset_scope == "application":
            clauses.extend(
                (
                    Skill.asset_scope == "application",
                    Skill.application_agent_id == command.application_agent_id,
                )
            )
        elif command.visibility == "private":
            clauses.append(Skill.owner_user_id == command.owner_user_id)
        else:
            clauses.append(Skill.visibility != "private")
        existing = await self.db.scalar(select(Skill).where(*clauses))
        if existing is not None:
            raise AssetCatalogError("SKILL_SLUG_EXISTS", "Skill slug already exists")
        skill = Skill(
            org_id=command.org_id,
            owner_user_id=command.owner_user_id,
            application_agent_id=command.application_agent_id,
            slug=command.slug,
            name=command.name,
            description=command.description,
            category_key=command.category_key,
            asset_scope=command.asset_scope,
            visibility=command.visibility,
            status="published" if command.asset_scope == "public" else "private",
        )
        self.db.add(skill)
        await self.db.flush()
        return await self._append_skill_version(
            skill=skill,
            material=command.material,
        )

    async def publish_skill_version(
        self,
        command: PublishSkillVersionCommand,
    ) -> AssetCatalogResult:
        skill = await self.db.scalar(
            select(Skill)
            .where(
                Skill.id == command.skill_id,
                Skill.org_id == command.org_id,
                Skill.status.notin_(("deleted", "revoked")),
            )
            .with_for_update()
        )
        if skill is None or skill.revoked_at is not None:
            raise AssetCatalogError("SKILL_UNAVAILABLE", "Skill is unavailable")
        if skill.archived_at is not None:
            raise AssetCatalogError("SKILL_ARCHIVED", "Archived Skill cannot be updated")
        return await self._append_skill_version(skill=skill, material=command.material)

    async def set_skill_market_visible(
        self, *, org_id: int, skill_id: int, visible: bool
    ) -> Skill:
        skill = await self._locked_skill(org_id=org_id, skill_id=skill_id)
        if str(skill.asset_scope) != "public" or str(skill.status) == "deleted":
            raise AssetCatalogError("SKILL_UNAVAILABLE", "Skill is unavailable")
        skill.market_visible = bool(visible)
        # A public Skill remains executable and keeps its stable identity while
        # hidden from the market.
        if visible and skill.current_version_id is not None:
            skill.status = "published"
        await self.db.flush()
        return skill

    async def republish_skill(self, *, org_id: int, skill_id: int) -> AssetCatalogResult:
        skill = await self._locked_skill(org_id=org_id, skill_id=skill_id)
        if str(skill.asset_scope) != "public" or str(skill.status) == "deleted":
            raise AssetCatalogError("SKILL_UNAVAILABLE", "Skill is unavailable")
        if skill.current_version_id is None:
            raise AssetCatalogError("SKILL_VERSION_UNAVAILABLE", "Skill has no current version")
        skill.market_visible = True
        skill.status = "published"
        version = await self.db.get(SkillVersion, int(skill.current_version_id))
        if version is None:
            raise AssetCatalogError("SKILL_VERSION_UNAVAILABLE", "Skill version is unavailable")
        await self.db.flush()
        return self._skill_result(skill=skill, version=version, created=False)

    async def _append_skill_version(
        self,
        *,
        skill: Skill,
        material: SkillVersionMaterial,
    ) -> AssetCatalogResult:
        content_digest = f"sha256:{material.package_sha256}"
        existing = await self.db.scalar(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id,
                SkillVersion.package_sha256 == material.package_sha256,
                SkillVersion.status != "deleted",
            )
        )
        if existing is not None:
            skill.current_version_id = int(existing.id)
            skill.status = "published" if str(skill.asset_scope) == "public" else "private"
            return self._skill_result(
                skill=skill,
                version=existing,
                created=False,
            )
        next_version = int(
            await self.db.scalar(
                select(func.max(SkillVersion.version)).where(
                    SkillVersion.skill_id == skill.id
                )
            )
            or 0
        ) + 1
        version = SkillVersion(
            org_id=int(skill.org_id),
            skill_id=int(skill.id),
            version=next_version,
            package_uri=material.package_uri,
            package_sha256=material.package_sha256,
            manifest=material.manifest,
            status="published" if str(skill.asset_scope) == "public" else "private",
            published_at=datetime.utcnow(),
        )
        self.db.add(version)
        await self.db.flush()
        skill.current_version_id = int(version.id)
        skill.status = "published" if str(skill.asset_scope) == "public" else "private"
        await self.db.flush()
        return AssetCatalogResult(
            asset_type="skill",
            asset_id=int(skill.id),
            version_id=int(version.id),
            version=next_version,
            created_version=True,
            content_digest=content_digest,
        )

    async def create_mcp(
        self,
        command: CreateMcpAssetCommand,
    ) -> AssetCatalogResult:
        namespace_key = mcp_slug_namespace(
            visibility=command.visibility,
            owner_user_id=command.owner_user_id,
        )
        active_lease = await self.db.scalar(
            select(McpSlugLease.id).where(
                McpSlugLease.org_id == command.org_id,
                McpSlugLease.namespace_key == namespace_key,
                McpSlugLease.slug == command.slug,
            )
        )
        if active_lease is not None or await self._conflicting_mcp_exists(command):
            raise AssetCatalogError("MCP_SLUG_EXISTS", "MCP slug already exists")
        mcp = Mcp(
            org_id=command.org_id,
            owner_user_id=command.owner_user_id,
            slug=command.slug,
            name=command.name,
            description=command.description,
            visibility=command.visibility,
            transport=command.material.transport,
            config={},
            status="private" if command.visibility == "private" else "published",
        )
        try:
            async with self.db.begin_nested():
                self.db.add(mcp)
                await self.db.flush()
                self.db.add(
                    McpSlugLease(
                        org_id=command.org_id,
                        namespace_key=namespace_key,
                        slug=command.slug,
                        mcp_id=int(mcp.id),
                    )
                )
                await self.db.flush()
        except IntegrityError as exc:
            raise AssetCatalogError(
                "MCP_SLUG_EXISTS",
                "MCP slug already exists",
            ) from exc
        return await self._append_mcp_version(mcp=mcp, material=command.material)

    async def _conflicting_mcp_exists(
        self,
        command: CreateMcpAssetCommand,
    ) -> bool:
        """Fail closed until every Runtime uses mcp_id-based aliases."""

        clauses = [
            Mcp.org_id == command.org_id,
            Mcp.slug == command.slug,
            Mcp.status != "deleted",
        ]
        if settings.MCP_RUNTIME_IDENTITY_MODE == "asset_id":
            clauses.extend(
                (
                    Mcp.status.notin_(("deleted", "revoked")),
                    Mcp.revoked_at.is_(None),
                )
            )
        if command.visibility == "private":
            clauses.extend(
                (
                    Mcp.visibility == "private",
                    Mcp.owner_user_id == command.owner_user_id,
                )
            )
        else:
            clauses.append(Mcp.visibility != "private")
        return await self.db.scalar(select(Mcp.id).where(*clauses)) is not None

    async def publish_mcp_version(
        self,
        command: PublishMcpVersionCommand,
    ) -> AssetCatalogResult:
        mcp = await self.db.scalar(
            select(Mcp)
            .where(
                Mcp.id == command.mcp_id,
                Mcp.org_id == command.org_id,
                Mcp.status.notin_(("deleted", "revoked")),
            )
            .with_for_update()
        )
        if mcp is None or mcp.revoked_at is not None:
            raise AssetCatalogError("MCP_UNAVAILABLE", "MCP is unavailable")
        if mcp.archived_at is not None:
            raise AssetCatalogError("MCP_ARCHIVED", "Archived MCP cannot be updated")
        if (
            command.expected_current_version_id is not None
            and int(mcp.current_version_id or 0)
            != command.expected_current_version_id
        ):
            raise AssetCatalogError(
                "MCP_VERSION_CONFLICT",
                "MCP current version changed",
            )
        if command.name is not None:
            mcp.name = command.name
        if "description" in command.model_fields_set:
            mcp.description = command.description
        return await self._append_mcp_version(mcp=mcp, material=command.material)

    async def _append_mcp_version(
        self,
        *,
        mcp: Mcp,
        material: McpVersionMaterial,
    ) -> AssetCatalogResult:
        normalized = {
            "transport": material.transport,
            "connection_config": material.connection_config,
            "secret_refs": material.secret_refs,
            "tools_schema": material.tools_schema,
        }
        config_digest = _canonical_digest(normalized)
        existing = await self.db.scalar(
            select(McpVersion).where(
                McpVersion.mcp_id == mcp.id,
                McpVersion.config_digest == config_digest,
                McpVersion.status != "deleted",
            )
        )
        if existing is not None:
            mcp.current_version_id = int(existing.id)
            mcp.transport = existing.transport
            mcp.config = {}
            mcp.status = "private" if str(mcp.visibility) == "private" else "published"
            return self._mcp_result(mcp=mcp, version=existing, created=False)
        next_version = int(
            await self.db.scalar(
                select(func.max(McpVersion.version)).where(McpVersion.mcp_id == mcp.id)
            )
            or 0
        ) + 1
        tools_schema_digest = (
            _canonical_digest(material.tools_schema)
            if material.tools_schema is not None
            else None
        )
        version = McpVersion(
            org_id=int(mcp.org_id),
            mcp_id=int(mcp.id),
            version=next_version,
            transport=material.transport,
            connection_config=material.connection_config,
            secret_refs=material.secret_refs or None,
            config_digest=config_digest,
            tools_schema=material.tools_schema,
            tools_schema_digest=tools_schema_digest,
            status="published",
            published_at=datetime.utcnow(),
        )
        self.db.add(version)
        await self.db.flush()
        mcp.current_version_id = int(version.id)
        mcp.transport = material.transport
        mcp.config = {}
        mcp.status = "private" if str(mcp.visibility) == "private" else "published"
        await self.db.flush()
        return self._mcp_result(mcp=mcp, version=version, created=True)

    async def archive_skill(self, *, org_id: int, skill_id: int) -> None:
        skill = await self._locked_skill(org_id=org_id, skill_id=skill_id)
        skill.archived_at = skill.archived_at or datetime.utcnow()
        skill.status = "archived"

    async def set_skill_execution_enabled(
        self, *, org_id: int, skill_id: int, enabled: bool
    ) -> Skill:
        skill = await self._locked_skill(org_id=org_id, skill_id=skill_id)
        if str(skill.status) == "deleted":
            raise AssetCatalogError("SKILL_UNAVAILABLE", "Skill is unavailable")
        skill.archived_at = None
        skill.status = "published" if str(skill.asset_scope) == "public" else "private"
        skill.revoked_at = None if enabled else (skill.revoked_at or datetime.utcnow())
        return skill

    async def revoke_skill(self, *, org_id: int, skill_id: int) -> None:
        skill = await self._locked_skill(org_id=org_id, skill_id=skill_id)
        skill.revoked_at = skill.revoked_at or datetime.utcnow()
        skill.status = "revoked"

    async def archive_mcp(self, *, org_id: int, mcp_id: int) -> None:
        mcp = await self._locked_mcp(org_id=org_id, mcp_id=mcp_id)
        mcp.archived_at = mcp.archived_at or datetime.utcnow()
        mcp.status = "archived"

    async def set_mcp_execution_enabled(
        self, *, org_id: int, mcp_id: int, enabled: bool
    ) -> Mcp:
        mcp = await self._locked_mcp(org_id=org_id, mcp_id=mcp_id)
        if str(mcp.status) == "deleted":
            raise AssetCatalogError("MCP_UNAVAILABLE", "MCP is unavailable")
        mcp.archived_at = None
        mcp.status = "private" if str(mcp.visibility) == "private" else "published"
        mcp.revoked_at = None if enabled else (mcp.revoked_at or datetime.utcnow())
        return mcp

    async def revoke_mcp(self, *, org_id: int, mcp_id: int) -> None:
        mcp = await self._locked_mcp(org_id=org_id, mcp_id=mcp_id)
        mcp.revoked_at = mcp.revoked_at or datetime.utcnow()
        mcp.status = "revoked"
        await self.db.execute(
            delete(McpSlugLease).where(
                McpSlugLease.org_id == org_id,
                McpSlugLease.mcp_id == mcp_id,
            )
        )

    async def permanently_delete_public_skill(
        self, *, org_id: int, skill_id: int
    ) -> Skill:
        skill = await self._locked_skill(org_id=org_id, skill_id=skill_id)
        if str(skill.asset_scope) != "public":
            raise AssetCatalogError("SKILL_UNAVAILABLE", "Skill is unavailable")
        if str(skill.status) == "deleted":
            return skill
        skill.slug = _deleted_skill_slug(str(skill.slug), int(skill.id))
        skill.status = "deleted"
        skill.revoked_at = skill.revoked_at or datetime.utcnow()
        return skill

    async def permanently_delete_public_mcp(
        self, *, org_id: int, mcp_id: int
    ) -> Mcp:
        mcp = await self._locked_mcp(org_id=org_id, mcp_id=mcp_id)
        if str(mcp.visibility) == "private":
            raise AssetCatalogError("MCP_UNAVAILABLE", "MCP is unavailable")
        if str(mcp.status) != "deleted":
            mcp.status = "deleted"
            mcp.revoked_at = mcp.revoked_at or datetime.utcnow()
        await self.db.execute(
            delete(McpSlugLease).where(
                McpSlugLease.org_id == org_id,
                McpSlugLease.mcp_id == mcp_id,
            )
        )
        return mcp

    async def _locked_skill(self, *, org_id: int, skill_id: int) -> Skill:
        skill = await self.db.scalar(
            select(Skill)
            .where(Skill.id == skill_id, Skill.org_id == org_id)
            .with_for_update()
        )
        if skill is None:
            raise AssetCatalogError("SKILL_UNAVAILABLE", "Skill is unavailable")
        return skill

    async def _locked_mcp(self, *, org_id: int, mcp_id: int) -> Mcp:
        mcp = await self.db.scalar(
            select(Mcp)
            .where(Mcp.id == mcp_id, Mcp.org_id == org_id)
            .with_for_update()
        )
        if mcp is None:
            raise AssetCatalogError("MCP_UNAVAILABLE", "MCP is unavailable")
        return mcp

    @staticmethod
    def _skill_result(
        *,
        skill: Skill,
        version: SkillVersion,
        created: bool,
    ) -> AssetCatalogResult:
        return AssetCatalogResult(
            asset_type="skill",
            asset_id=int(skill.id),
            version_id=int(version.id),
            version=int(version.version),
            created_version=created,
            content_digest=f"sha256:{str(version.package_sha256)}",
        )

    @staticmethod
    def _mcp_result(
        *,
        mcp: Mcp,
        version: McpVersion,
        created: bool,
    ) -> AssetCatalogResult:
        return AssetCatalogResult(
            asset_type="mcp",
            asset_id=int(mcp.id),
            version_id=int(version.id),
            version=int(version.version),
            created_version=created,
            content_digest=str(version.config_digest),
        )
