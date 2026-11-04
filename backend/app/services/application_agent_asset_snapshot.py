from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationAgentPersonalKnowledgeBinding,
    ApplicationAgentSkill,
    Mcp,
    McpVersion,
    Skill,
    SkillVersion,
)
from app.services.plaza.public_catalog.package_parser import normalize_skill_name


class ApplicationAgentAssetSnapshotError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _positive_ids(values: object, *, code: str) -> list[int]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ApplicationAgentAssetSnapshotError(code)
    result: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise ApplicationAgentAssetSnapshotError(code)
        try:
            item = int(value)
        except (TypeError, ValueError) as exc:
            raise ApplicationAgentAssetSnapshotError(code) from exc
        if item <= 0 or item in result:
            raise ApplicationAgentAssetSnapshotError(code)
        result.append(item)
    return result


class ApplicationAgentAssetSnapshotBuilder:
    """Resolve application-owned/public assets to an immutable revision fact."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(
        self,
        *,
        org_id: int,
        application_agent_id: int,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        skill_ids = _positive_ids(
            config.get("skill_asset_ids"),
            code="application_agent_skill_config_invalid",
        )
        private_skill_ids = _positive_ids(
            config.get("application_skill_ids"),
            code="application_agent_private_skill_config_invalid",
        )
        mcp_ids = _positive_ids(
            config.get("mcp_asset_ids"),
            code="application_agent_mcp_config_invalid",
        )
        knowledge_ids = [
            int(item)
            for item in (
                await self.db.execute(
                    select(
                        ApplicationAgentPersonalKnowledgeBinding
                        .knowledge_base_id
                    )
                    .where(
                        ApplicationAgentPersonalKnowledgeBinding.org_id
                        == org_id,
                        ApplicationAgentPersonalKnowledgeBinding
                        .application_agent_id == application_agent_id,
                        ApplicationAgentPersonalKnowledgeBinding.status
                        == "enabled",
                    )
                    .order_by(
                        ApplicationAgentPersonalKnowledgeBinding
                        .knowledge_base_id
                    )
                )
            ).scalars()
        ]

        skills = await self._public_skills(
            org_id=org_id,
            skill_ids=skill_ids,
            existing_skill_ids=self._existing_public_skill_ids(config),
        )
        private_skills = await self._private_skills(
            org_id=org_id,
            application_agent_id=application_agent_id,
            skill_ids=private_skill_ids,
        )
        await self._validate_skill_names(
            org_id=org_id,
            application_agent_id=application_agent_id,
            public_skill_ids=skill_ids,
            private_skill_ids=private_skill_ids,
        )
        mcps = await self._public_mcps(org_id=org_id, mcp_ids=mcp_ids)

        snapshot = {
            "schema_version": 1,
            "skills": skills,
            "application_skills": private_skills,
            "mcps": mcps,
            "knowledge_base_ids": knowledge_ids,
        }
        result = dict(config)
        result["asset_snapshot"] = snapshot
        return result

    async def _validate_skill_names(
        self,
        *,
        org_id: int,
        application_agent_id: int,
        public_skill_ids: list[int],
        private_skill_ids: list[int],
    ) -> None:
        if not public_skill_ids and not private_skill_ids:
            return
        rows = (
            await self.db.execute(
                select(Skill.id, Skill.name).where(
                    Skill.id.in_([*public_skill_ids, *private_skill_ids]),
                    Skill.org_id == org_id,
                    (
                        (Skill.asset_scope == "public")
                        | (
                            (Skill.asset_scope == "application")
                            & (Skill.application_agent_id == application_agent_id)
                        )
                    ),
                    Skill.status != "deleted",
                )
            )
        ).all()
        names: set[str] = set()
        for _skill_id, name in rows:
            normalized = normalize_skill_name(str(name))
            if normalized in names:
                raise ApplicationAgentAssetSnapshotError(
                    "application_agent_skill_name_conflict"
                )
            names.add(normalized)

    async def _public_skills(
        self,
        *,
        org_id: int,
        skill_ids: list[int],
        existing_skill_ids: set[int],
    ) -> list[dict[str, Any]]:
        if not skill_ids:
            return []
        rows = list(
            (
                await self.db.execute(
                    select(Skill).where(
                        Skill.id.in_(skill_ids),
                        Skill.org_id == org_id,
                        Skill.asset_scope == "public",
                        Skill.status == "published",
                        Skill.archived_at.is_(None),
                        Skill.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
        by_id = {int(row.id): row for row in rows}
        versions = list(
            (
                await self.db.execute(
                    select(SkillVersion).where(
                        SkillVersion.org_id == org_id,
                        SkillVersion.skill_id.in_(skill_ids),
                        SkillVersion.status == "published",
                    )
                )
            ).scalars()
        )
        by_version_id = {int(row.id): row for row in versions}
        result: list[dict[str, Any]] = []
        for skill_id in skill_ids:
            skill = by_id.get(skill_id)
            version = (
                by_version_id.get(int(skill.current_version_id))
                if skill is not None and skill.current_version_id is not None
                else None
            )
            if skill is None or version is None:
                raise ApplicationAgentAssetSnapshotError(
                    "application_agent_skill_not_active"
                )
            if not bool(skill.market_visible) and skill_id not in existing_skill_ids:
                raise ApplicationAgentAssetSnapshotError(
                    "application_agent_skill_not_market_visible"
                )
            result.append(
                {
                    "asset_id": skill_id,
                    "version_id": int(version.id),
                    "sha256": str(version.package_sha256 or ""),
                }
            )
        return result

    @staticmethod
    def _existing_public_skill_ids(config: Mapping[str, Any]) -> set[int]:
        # This builder is also used for refreshes. Existing snapshots are
        # intentionally allowed to resolve after market unpublish.
        snapshot = config.get("asset_snapshot")
        if not isinstance(snapshot, Mapping):
            return set()
        skills = snapshot.get("skills")
        if not isinstance(skills, list):
            return set()
        return {
            int(item["asset_id"])
            for item in skills
            if isinstance(item, Mapping) and str(item.get("asset_id", "")).isdigit()
        }

    async def _private_skills(
        self,
        *,
        org_id: int,
        application_agent_id: int,
        skill_ids: list[int],
    ) -> list[dict[str, Any]]:
        if not skill_ids:
            return []
        unified_rows = list(
            (
                await self.db.execute(
                    select(Skill, SkillVersion)
                    .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
                    .where(
                        Skill.id.in_(skill_ids),
                        Skill.org_id == org_id,
                        Skill.application_agent_id == application_agent_id,
                        Skill.asset_scope == "application",
                        Skill.status != "deleted",
                        SkillVersion.status != "deleted",
                    )
                )
            ).all()
        )
        unified_by_id = {
            int(skill.id): (skill, version)
            for skill, version in unified_rows
        }
        missing_ids = [item for item in skill_ids if item not in unified_by_id]
        legacy_rows = list(
            (
                await self.db.execute(
                    select(ApplicationAgentSkill).where(
                        ApplicationAgentSkill.id.in_(missing_ids),
                        ApplicationAgentSkill.org_id == org_id,
                        ApplicationAgentSkill.application_agent_id
                        == application_agent_id,
                    )
                )
            ).scalars()
        ) if missing_ids else []
        legacy_by_id = {int(row.id): row for row in legacy_rows}
        result: list[dict[str, Any]] = []
        for skill_id in skill_ids:
            unified = unified_by_id.get(skill_id)
            if unified is not None:
                _skill, version = unified
                result.append(
                    {
                        "asset_id": skill_id,
                        "version_id": int(version.id),
                        "sha256": str(version.package_sha256 or ""),
                    }
                )
                continue
            legacy = legacy_by_id.get(skill_id)
            if legacy is None:
                raise ApplicationAgentAssetSnapshotError(
                    "application_agent_private_skill_not_found"
                )
            result.append(
                {
                    "asset_id": skill_id,
                    "version_id": skill_id,
                    "sha256": str(legacy.package_sha256),
                }
            )
        return result

    async def _public_mcps(
        self,
        *,
        org_id: int,
        mcp_ids: list[int],
    ) -> list[dict[str, Any]]:
        if not mcp_ids:
            return []
        rows = list(
            (
                await self.db.execute(
                    select(Mcp).where(
                        Mcp.id.in_(mcp_ids),
                        Mcp.org_id == org_id,
                        Mcp.visibility != "private",
                        Mcp.status == "published",
                        Mcp.archived_at.is_(None),
                        Mcp.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )
        by_id = {int(row.id): row for row in rows}
        versions = list(
            (
                await self.db.execute(
                    select(McpVersion).where(
                        McpVersion.org_id == org_id,
                        McpVersion.mcp_id.in_(mcp_ids),
                        McpVersion.status == "published",
                    )
                )
            ).scalars()
        )
        by_version_id = {int(row.id): row for row in versions}
        result: list[dict[str, Any]] = []
        for mcp_id in mcp_ids:
            mcp = by_id.get(mcp_id)
            version = (
                by_version_id.get(int(mcp.current_version_id))
                if mcp is not None and mcp.current_version_id is not None
                else None
            )
            if mcp is None or version is None:
                raise ApplicationAgentAssetSnapshotError(
                    "application_agent_mcp_not_active"
                )
            result.append(
                {
                    "asset_id": mcp_id,
                    "version_id": int(version.id),
                    "config_digest": str(version.config_digest),
                }
            )
        return result


__all__ = [
    "ApplicationAgentAssetSnapshotBuilder",
    "ApplicationAgentAssetSnapshotError",
]
