"""Read-only capability catalogue used by the expert authoring UI."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    KnowledgeBase,
    Mcp,
    Permission,
    ProfileMcpBinding,
    ProfileSkillBinding,
    Skill,
    SkillCategory,
    SkillVersion,
    SysDept,
    SysUser,
    User,
)
from app.schemas.experts import (
    ExpertCapabilityCatalog,
    ExpertCapabilityCatalogItem,
    ExpertSkillCategory,
)
from app.services.plaza.user_asset_scope import (
    UserAssetScopeError,
    resolve_default_user_asset_scope,
)
from app.services.runtime_workspace import Principal


APPROVED_EXPERT_PLUGIN_IDS = frozenset({"enterprise-knowledge", "personal-knowledge"})


class ExpertCapabilityCatalogQueryService:
    """Build the selectable asset list without installing or projecting anything."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for(self, *, principal: Principal) -> ExpertCapabilityCatalog:
        skills = (
            await self.db.execute(
                select(Skill, SkillVersion)
                .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
                .where(
                    Skill.org_id == principal.org_id,
                    Skill.asset_scope == "public",
                    Skill.visibility != "private",
                    Skill.market_visible.is_(True),
                    Skill.status == "published",
                    Skill.revoked_at.is_(None),
                    SkillVersion.status == "published",
                )
                .order_by(Skill.name.asc(), Skill.id.asc())
            )
        ).all()
        mcps = (
            (
                await self.db.execute(
                    select(Mcp)
                    .where(
                        Mcp.org_id == principal.org_id,
                        Mcp.visibility != "private",
                        Mcp.status == "published",
                        Mcp.revoked_at.is_(None),
                    )
                    .order_by(Mcp.name.asc(), Mcp.id.asc())
                )
            )
            .scalars()
            .all()
        )
        knowledge_ids = await self._visible_knowledge_base_ids(principal=principal)
        knowledge_bases = (
            (
                await self.db.execute(
                    select(KnowledgeBase)
                    .where(KnowledgeBase.id.in_(knowledge_ids))
                    .order_by(KnowledgeBase.name.asc(), KnowledgeBase.id.asc())
                )
            )
            .scalars()
            .all()
            if knowledge_ids
            else []
        )
        try:
            default_scope = await resolve_default_user_asset_scope(
                self.db,
                org_id=principal.org_id,
                user_id=principal.user_id,
            )
        except UserAssetScopeError:
            default_scope = None
        installed_skills: list[tuple[ProfileSkillBinding, Skill, SkillVersion]] = []
        installed_mcps: list[tuple[ProfileMcpBinding, Mcp]] = []
        if default_scope is not None:
            installed_skills = list(
                (
                    await self.db.execute(
                        select(ProfileSkillBinding, Skill, SkillVersion)
                        .join(Skill, Skill.id == ProfileSkillBinding.skill_id)
                        .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
                        .where(
                            ProfileSkillBinding.org_id == principal.org_id,
                            ProfileSkillBinding.runtime_workspace_id
                            == default_scope.runtime_workspace_id,
                            ProfileSkillBinding.runtime_agent_profile_id
                            == default_scope.runtime_agent_profile_id,
                            ProfileSkillBinding.status == "enabled",
                        )
                        .order_by(Skill.name.asc(), Skill.id.asc())
                    )
                ).all()
            )
            installed_mcps = list(
                (
                    await self.db.execute(
                        select(ProfileMcpBinding, Mcp)
                        .join(Mcp, Mcp.id == ProfileMcpBinding.mcp_id)
                        .where(
                            ProfileMcpBinding.org_id == principal.org_id,
                            ProfileMcpBinding.runtime_workspace_id
                            == default_scope.runtime_workspace_id,
                            ProfileMcpBinding.runtime_agent_profile_id
                            == default_scope.runtime_agent_profile_id,
                            ProfileMcpBinding.status == "enabled",
                            Mcp.status == "published",
                        )
                        .order_by(Mcp.name.asc(), Mcp.id.asc())
                    )
                ).all()
            )
        categories = list(
            (
                await self.db.execute(
                    select(SkillCategory)
                    .where(SkillCategory.org_id == principal.org_id)
                    .order_by(
                        SkillCategory.is_builtin.desc(),
                        SkillCategory.category_key.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        personal_skills = (
            await self.db.execute(
                select(Skill, SkillVersion)
                .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
                .where(
                    Skill.org_id == principal.org_id,
                    Skill.owner_user_id == principal.user_id,
                    Skill.asset_scope == "personal",
                    Skill.status.in_(("private", "draft", "rejected")),
                    SkillVersion.status != "deleted",
                )
                .order_by(Skill.name.asc(), Skill.id.asc())
            )
        ).all()
        personal_knowledge_bases = [
            item
            for item in knowledge_bases
            if int(item.owner_user_id) == principal.user_id
            and str(item.kind).lower() == "personal"
        ]
        public_knowledge_bases = [
            item for item in knowledge_bases if item not in personal_knowledge_bases
        ]
        return ExpertCapabilityCatalog(
            market_skills=[
                ExpertCapabilityCatalogItem(
                    asset_id=int(skill.id),
                    version_id=int(version.id),
                    name=str(skill.name),
                    description=skill.description,
                    category_key=str(
                        (version.manifest or {}).get("category_key") or "other"
                    ),
                )
                for skill, version in skills
            ],
            installed_skills=[
                ExpertCapabilityCatalogItem(
                    asset_id=int(skill.id),
                    version_id=int(version.id),
                    name=str(skill.name),
                    description=skill.description,
                    source="default_installed",
                    category_key=str(
                        (version.manifest or {}).get("category_key") or "other"
                    ),
                    runtime_skill_name=str(skill.slug),
                )
                for _binding, skill, version in installed_skills
            ],
            skill_categories=[
                ExpertSkillCategory(
                    category_key=str(item.category_key),
                    display_name=str(item.display_name),
                )
                for item in categories
            ],
            market_mcps=[
                ExpertCapabilityCatalogItem(
                    asset_id=int(item.id),
                    name=str(item.name),
                    description=item.description,
                    kind=str(item.transport),
                )
                for item in mcps
            ],
            installed_mcps=[
                ExpertCapabilityCatalogItem(
                    asset_id=int(item.id),
                    name=str(item.name),
                    description=item.description,
                    kind=str(item.transport),
                    source="default_installed",
                )
                for _binding, item in installed_mcps
            ],
            knowledge_bases=[
                ExpertCapabilityCatalogItem(
                    asset_id=int(item.id),
                    name=str(item.name),
                    description=item.description,
                    kind=str(item.kind),
                )
                for item in public_knowledge_bases
            ],
            personal_skills=[
                ExpertCapabilityCatalogItem(
                    asset_id=int(skill.id),
                    version_id=int(version.id),
                    name=str(skill.name),
                    description=skill.description,
                    source="personal",
                )
                for skill, version in personal_skills
            ],
            personal_knowledge_bases=[
                ExpertCapabilityCatalogItem(
                    asset_id=int(item.id),
                    name=str(item.name),
                    description=item.description,
                    kind=str(item.kind),
                    source="personal",
                )
                for item in personal_knowledge_bases
            ],
            plugin_ids=sorted(APPROVED_EXPERT_PLUGIN_IDS),
        )

    async def _visible_knowledge_base_ids(self, *, principal: Principal) -> set[int]:
        knowledge_bases = list(
            (
                await self.db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.org_id == principal.org_id,
                        KnowledgeBase.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        candidate_ids = {int(item.id) for item in knowledge_bases}
        if not candidate_ids:
            return set()
        department_ids = await self._department_ids(principal)
        target_filters = [
            (Permission.grantee_type == "user")
            & (Permission.grantee_id == str(principal.user_id)),
            (Permission.grantee_type == "all") & (Permission.grantee_id == "*"),
        ]
        if department_ids:
            target_filters.append(
                (Permission.grantee_type == "department")
                & (Permission.grantee_id.in_(department_ids))
            )
        permitted_ids = set(
            (
                await self.db.execute(
                    select(Permission.asset_id).where(
                        Permission.org_id == principal.org_id,
                        Permission.asset_type == "knowledge_base",
                        Permission.asset_id.in_(candidate_ids),
                        or_(*target_filters),
                        or_(
                            Permission.kb_role.is_(None),
                            Permission.kb_role.in_(
                                ("retrieve", "preview", "download", "owner")
                            ),
                        ),
                    )
                )
            ).scalars()
        )
        return {
            int(item.id)
            for item in knowledge_bases
            if int(item.owner_user_id) == principal.user_id
            or str(item.visibility).lower() in {"organization", "org", "public"}
            or int(item.id) in permitted_ids
        }

    async def _department_ids(self, principal: Principal) -> set[str]:
        user = await self.db.scalar(
            select(User).where(
                User.id == principal.user_id,
                User.org_id == principal.org_id,
                User.status == "active",
            )
        )
        if user is None or not user.user_code:
            return set()
        directory_user = await self.db.scalar(select(SysUser).where(SysUser.code == user.user_code))
        current = str(directory_user.dept_id or "") if directory_user else ""
        department_ids: set[str] = set()
        if directory_user is not None and directory_user.com_id:
            department_ids.add(str(directory_user.com_id))
        while current and current not in department_ids:
            department_ids.add(current)
            current = str(
                await self.db.scalar(select(SysDept.parent_id).where(SysDept.dept_id == current))
                or ""
            )
        return department_ids


__all__ = ["ExpertCapabilityCatalogQueryService"]
