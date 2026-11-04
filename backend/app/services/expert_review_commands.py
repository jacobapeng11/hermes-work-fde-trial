from __future__ import annotations

from datetime import datetime, timezone
import logging

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    InstalledExpertBinding,
    KnowledgeBase,
    Mcp,
    Permission,
    PlazaTemplate,
    Skill,
    SkillVersion,
    SysDept,
    SysUser,
    TemplateReview,
    TemplateVersion,
    User,
)
from app.schemas.experts import (
    ApproveExpertReviewRequest,
    ExpertCapabilitySelection,
    ExpertReviewSummary,
    ExpertShareScope,
)
from app.schemas.personal_knowledge import PersonalKnowledgeSelectedUser
from app.services.expert_review import ExpertReviewError, ExpertReviewService
from app.services.runtime_workspace import Principal
from app.services.expert_profile_queries import ExpertProfileSummaryQueryService
from app.services.expert_errors import ExpertProfileError
from app.services.user_directory import COMPANY_ROOT_DEPARTMENT_IDS


logger = logging.getLogger(__name__)


APPROVED_EXPERT_PLUGIN_IDS = frozenset(
    {
        "enterprise-knowledge",
        "personal-knowledge",
    }
)
NONTERMINAL_RUN_STATUSES = frozenset({"accepted", "pending", "running"})

class ExpertReviewCommandService:
    """Own review submission decisions and immutable market publication commands."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.summary_queries = ExpertProfileSummaryQueryService(db)

    async def list_admin_reviews(
        self,
        *,
        org_id: int,
        status: str | None = None,
    ) -> list[ExpertReviewSummary]:
        clauses = [
            TemplateReview.org_id == org_id,
            or_(
                TemplateReview.status != "approved",
                PlazaTemplate.status == "published",
            ),
        ]
        if status:
            clauses.append(TemplateReview.status == status)
        reviews = (
            (
                await self.db.execute(
                    select(TemplateReview)
                    .outerjoin(
                        PlazaTemplate,
                        TemplateReview.template_id == PlazaTemplate.id,
                    )
                    .where(*clauses)
                    .order_by(TemplateReview.created_at.desc(), TemplateReview.id.desc())
                )
            )
            .scalars()
            .all()
        )
        result: list[ExpertReviewSummary] = []
        for review in reviews:
            if dict(review.review_snapshot or {}).get("schema_version") != "expert.review/v2":
                continue
            result.append(await self._review_summary(review))
        return result


    async def approve_review(
        self,
        *,
        org_id: int,
        reviewer_user_id: int,
        review_id: int,
        command: ApproveExpertReviewRequest,
    ) -> ExpertReviewSummary:
        review = await self._pending_review(org_id=org_id, review_id=review_id)
        principal = Principal(org_id=org_id, user_id=review.submitter_user_id)
        approved_scope = self._scope_from_review(review)
        if approved_scope.is_private:
            raise ExpertProfileError("expert_share_scope_required", status_code=409)
        await self._validate_share_scope(principal=principal, scope=approved_scope)
        package = await self.db.get(ExpertPackage, review.expert_package_id)
        version = await self.db.get(
            ExpertPackageVersion,
            review.expert_package_version_id,
        )
        if package is None or version is None:
            raise ExpertProfileError("expert_review_source_missing", status_code=409)
        await self._assert_release_artifacts_ready(version=version)
        try:
            await ExpertReviewService(self.db).acquire_market_name(
                package=package,
                version=version,
            )
        except ExpertReviewError as exc:
            raise ExpertProfileError(exc.code.lower(), status_code=409) from exc
        template = await self.db.scalar(
            select(PlazaTemplate).where(
                PlazaTemplate.org_id == org_id,
                PlazaTemplate.slug == package.slug,
            )
        )
        if template is None:
            template = PlazaTemplate(
                org_id=org_id,
                slug=package.slug,
                name=package.name,
                description=package.description,
                icon_url=package.avatar_url,
                category="expert",
                tags=[],
                visibility="restricted",
                status="draft",
                sort_order=0,
            )
            self.db.add(template)
            await self.db.flush()
        next_template_version = (
            int(
                await self.db.scalar(
                    select(func.max(TemplateVersion.version)).where(
                        TemplateVersion.template_id == template.id
                    )
                )
                or 0
            )
            + 1
        )
        template_version = TemplateVersion(
            org_id=org_id,
            template_id=int(template.id),
            version=next_template_version,
            source_expert_package_id=int(package.id),
            source_expert_package_version_id=int(version.id),
            config_snapshot={
                "schema_version": "expert.profile/v2",
                "basic_info": dict(version.basic_info_snapshot or {}),
                "soul": version.soul_snapshot or "",
                "plugin_refs": list(version.plugin_refs or []),
            },
            skill_snapshots=list(version.skill_refs or []),
            mcp_snapshots=list(version.mcp_refs or []),
            knowledge_snapshots=list(version.knowledge_refs or []),
            status="published",
            published_at=datetime.now(timezone.utc),
        )
        self.db.add(template_version)
        await self.db.flush()
        await self._replace_permissions(
            org_id=org_id,
            expert_package_id=int(package.id),
            scope=approved_scope,
        )
        template.name = package.name
        template.description = package.description
        template.icon_url = package.avatar_url
        template.current_version_id = int(template_version.id)
        template.visibility = "restricted"
        template.status = "published"
        review.template_id = int(template.id)
        snapshot = dict(review.review_snapshot or {})
        snapshot["approved_share_scope"] = approved_scope.model_dump()
        review.review_snapshot = snapshot
        await ExpertReviewService(self.db).approve(
            package=package,
            version=version,
            review=review,
            reviewer_user_id=reviewer_user_id,
            review_comment=command.review_comment,
        )
        await self.db.commit()
        return await self._review_summary(review)


    async def reject_review(
        self,
        *,
        org_id: int,
        reviewer_user_id: int,
        review_id: int,
        review_comment: str,
    ) -> ExpertReviewSummary:
        review = await self._pending_review(org_id=org_id, review_id=review_id)
        version = await self.db.get(
            ExpertPackageVersion,
            review.expert_package_version_id,
        )
        package = await self.db.get(ExpertPackage, review.expert_package_id)
        ExpertReviewService.reject(
            package=package,
            version=version,
            review=review,
            reviewer_user_id=reviewer_user_id,
            review_comment=review_comment,
        )
        await self.db.commit()
        return await self._review_summary(review)


    async def admin_unpublish(
        self,
        *,
        org_id: int,
        expert_package_id: int,
    ) -> None:
        package = await self.db.scalar(
            select(ExpertPackage).where(
                ExpertPackage.org_id == org_id,
                ExpertPackage.id == expert_package_id,
            )
        )
        if package is None:
            raise ExpertProfileError("expert_package_not_found", status_code=404)
        await self._unpublish_package(package)
        await self.db.commit()


    async def _validate_share_scope(
        self,
        *,
        principal: Principal,
        scope: ExpertShareScope,
    ) -> None:
        if scope.user_ids:
            valid_users = set(
                (
                    await self.db.execute(
                        select(User.id).where(
                            User.org_id == principal.org_id,
                            User.id.in_(scope.user_ids),
                            User.status == "active",
                        )
                    )
                ).scalars()
            )
            if valid_users != set(scope.user_ids):
                raise ExpertProfileError("invalid_expert_share_user")
        if scope.department_ids:
            if any(
                str(department_id).startswith("virtual:")
                for department_id in scope.department_ids
            ):
                raise ExpertProfileError("invalid_expert_share_department")
            requested = {
                str(department_id) for department_id in scope.department_ids
            }
            valid_departments = set(
                (
                    await self.db.execute(
                        select(SysDept.dept_id).where(
                            SysDept.dept_id.in_(requested),
                            SysDept.status == 1,
                        )
                    )
                ).scalars()
            )
            if (
                valid_departments | COMPANY_ROOT_DEPARTMENT_IDS
            ) & requested != requested:
                raise ExpertProfileError("invalid_expert_share_department")


    async def _validate_capability_share_coverage(
        self,
        *,
        org_id: int,
        scope: ExpertShareScope,
        knowledge_base_ids: list[int],
    ) -> None:
        """Ensure every market recipient can retrieve every private KB.

        Expert visibility may target a whole department (including its
        descendants), so a KB department grant only covers that target when it
        is on the same department or one of its ancestors.  This validation is
        deliberately performed at approval time because the administrator can
        change the final market scope.
        """
        if not knowledge_base_ids:
            return
        knowledge_bases = (
            (
                await self.db.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.org_id == org_id,
                        KnowledgeBase.id.in_(knowledge_base_ids),
                        KnowledgeBase.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        if {int(item.id) for item in knowledge_bases} != set(knowledge_base_ids):
            raise ExpertProfileError(
                "expert_capability_not_visible",
                status_code=403,
            )
        permissions = (
            (
                await self.db.execute(
                    select(Permission).where(
                        Permission.org_id == org_id,
                        Permission.asset_type == "knowledge_base",
                        Permission.asset_id.in_(knowledge_base_ids),
                        or_(
                            Permission.kb_role.is_(None),
                            Permission.kb_role.in_(("retrieve", "preview", "download", "owner")),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        permissions_by_asset: dict[int, list[Permission]] = {}
        for permission in permissions:
            permissions_by_asset.setdefault(
                int(permission.asset_id),
                [],
            ).append(permission)

        users = (
            (
                await self.db.execute(
                    select(User.id, User.user_code).where(
                        User.org_id == org_id,
                        User.id.in_(scope.user_ids),
                        User.status == "active",
                    )
                )
            ).all()
            if scope.user_ids
            else []
        )
        user_codes = {str(row.user_code): int(row.id) for row in users if row.user_code}
        directory_rows = (
            (
                await self.db.execute(
                    select(SysUser.code, SysUser.dept_id).where(
                        SysUser.code.in_(user_codes),
                    )
                )
            ).all()
            if user_codes
            else []
        )
        user_departments = {
            user_codes[str(row.code)]: str(row.dept_id or "")
            for row in directory_rows
            if row.code is not None
        }
        department_ancestors: dict[str, set[str]] = {}

        async def ancestors(department_id: str) -> set[str]:
            normalized = str(department_id or "")
            if not normalized:
                return set()
            cached = department_ancestors.get(normalized)
            if cached is not None:
                return cached
            result: set[str] = set()
            current = normalized
            while current and current not in result:
                result.add(current)
                current = str(
                    await self.db.scalar(
                        select(SysDept.parent_id).where(SysDept.dept_id == current)
                    )
                    or ""
                )
            department_ancestors[normalized] = result
            return result

        target_department_ancestors = {
            department_id: await ancestors(department_id) for department_id in scope.department_ids
        }
        target_user_ancestors = {
            user_id: await ancestors(user_departments.get(user_id, ""))
            for user_id in scope.user_ids
        }

        for knowledge_base in knowledge_bases:
            if str(knowledge_base.visibility).lower() in {
                "organization",
                "org",
                "public",
            }:
                continue
            asset_permissions = permissions_by_asset.get(
                int(knowledge_base.id),
                [],
            )
            if any(
                item.grantee_type == "all" and item.grantee_id == "*" for item in asset_permissions
            ):
                continue
            if scope.organization:
                raise ExpertProfileError(
                    "expert_capability_scope_not_covered",
                    status_code=409,
                )
            permitted_users = {
                int(item.grantee_id)
                for item in asset_permissions
                if item.grantee_type == "user" and str(item.grantee_id).isdigit()
            }
            permitted_departments = {
                str(item.grantee_id)
                for item in asset_permissions
                if item.grantee_type == "department"
            }
            users_covered = all(
                user_id == int(knowledge_base.owner_user_id)
                or user_id in permitted_users
                or bool(target_user_ancestors.get(user_id, set()) & permitted_departments)
                for user_id in scope.user_ids
            )
            departments_covered = all(
                bool(target_department_ancestors.get(department_id, set()) & permitted_departments)
                for department_id in scope.department_ids
            )
            if not users_covered or not departments_covered:
                raise ExpertProfileError(
                    "expert_capability_scope_not_covered",
                    status_code=409,
                )


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
        while current and current not in department_ids:
            department_ids.add(current)
            current = str(
                await self.db.scalar(select(SysDept.parent_id).where(SysDept.dept_id == current))
                or ""
            )
        return department_ids


    async def _replace_permissions(
        self,
        *,
        org_id: int,
        expert_package_id: int,
        scope: ExpertShareScope,
    ) -> None:
        await self.db.execute(
            delete(Permission).where(
                Permission.org_id == org_id,
                Permission.asset_type == "expert_package",
                Permission.asset_id == expert_package_id,
            )
        )
        if scope.organization:
            self.db.add(
                Permission(
                    org_id=org_id,
                    asset_type="expert_package",
                    asset_id=expert_package_id,
                    grantee_type="all",
                    grantee_id="*",
                    kb_role=None,
                )
            )
        for user_id in scope.user_ids:
            self.db.add(
                Permission(
                    org_id=org_id,
                    asset_type="expert_package",
                    asset_id=expert_package_id,
                    grantee_type="user",
                    grantee_id=str(user_id),
                    kb_role=None,
                )
            )
        for department_id in scope.department_ids:
            self.db.add(
                Permission(
                    org_id=org_id,
                    asset_type="expert_package",
                    asset_id=expert_package_id,
                    grantee_type="department",
                    grantee_id=department_id,
                    kb_role=None,
                )
            )


    @staticmethod
    def _scope_from_review(review: TemplateReview) -> ExpertShareScope:
        snapshot = dict(review.review_snapshot or {})
        scope = snapshot.get("approved_share_scope") or snapshot.get("requested_share_scope") or {}
        return ExpertShareScope.model_validate(scope)

    async def _review_summary(
        self,
        review: TemplateReview,
    ) -> ExpertReviewSummary:
        snapshot = dict(review.review_snapshot or {})
        basic = dict(snapshot.get("basic_info") or {})
        share_scope = self._scope_from_review(review)
        package = await self.db.get(ExpertPackage, int(review.expert_package_id))
        active_installation_count = int(
            await self.db.scalar(
                select(func.count(InstalledExpertBinding.id)).where(
                    InstalledExpertBinding.expert_package_id
                    == review.expert_package_id,
                    InstalledExpertBinding.status == "enabled",
                )
            )
            or 0
        )
        return ExpertReviewSummary(
            review_id=int(review.id),
            expert_package_id=int(review.expert_package_id),
            expert_package_version_id=int(review.expert_package_version_id or 0),
            submitter_user_id=int(review.submitter_user_id),
            name=str(basic.get("name") or ""),
            description=basic.get("description"),
            avatar_url=basic.get("avatar_url"),
            soul_content=str(snapshot.get("soul") or ""),
            system_prompt=(
                str(snapshot["system_prompt"])
                if snapshot.get("system_prompt") is not None
                else None
            ),
            capabilities=ExpertCapabilitySelection.model_validate(
                snapshot.get("capabilities") or {}
            ),
            share_scope=share_scope,
            share_scope_users=await self.summary_queries.selected_share_users(
                org_id=int(review.org_id),
                user_ids=share_scope.user_ids,
            ),
            change_summary=(
                str(snapshot["change_summary"])
                if snapshot.get("change_summary") is not None
                else None
            ),
            status=str(review.status),
            review_comment=review.review_comment,
            execution_status=(
                str(package.execution_status)
                if package is not None
                else "enabled"
            ),
            execution_status_reason=(
                package.execution_status_reason
                if package is not None
                else None
            ),
            active_installation_count=active_installation_count,
            created_at=review.created_at,
            reviewed_at=review.reviewed_at,
            updated_at=review.updated_at,
        )


    async def _selected_share_users(
        self,
        *,
        org_id: int,
        user_ids: list[int],
    ) -> list[PersonalKnowledgeSelectedUser]:
        if not user_ids:
            return []
        rows = (
            await self.db.execute(
                select(User.id, User.name, User.user_code, User.status).where(
                    User.org_id == org_id,
                    User.id.in_(user_ids),
                )
            )
        ).all()
        users_by_id = {int(row.id): row for row in rows}
        result: list[PersonalKnowledgeSelectedUser] = []
        for user_id in user_ids:
            row = users_by_id.get(user_id)
            if row is None:
                result.append(
                    PersonalKnowledgeSelectedUser(
                        id=user_id,
                        status="missing",
                    )
                )
                continue
            result.append(
                PersonalKnowledgeSelectedUser(
                    id=user_id,
                    name=str(row.name),
                    user_code=str(row.user_code) if row.user_code else None,
                    status="active" if row.status == "active" else "inactive",
                )
            )
        return result


    async def _prepare_release_artifacts(
        self,
        *,
        version: ExpertPackageVersion,
    ) -> None:
        """Validate stable release references without cloning personal assets."""
        package = await self.db.get(ExpertPackage, version.expert_package_id)
        if package is None:
            raise ValueError("expert package is unavailable for release")
        capabilities = ExpertProfileSummaryQueryService.capabilities_from_version(version)
        for reference in capabilities["skills"]:
            skill_id = int(reference["asset_id"])
            skill = await self.db.scalar(
                select(Skill).where(
                    Skill.id == skill_id,
                    Skill.org_id == version.org_id,
                    Skill.status.notin_(("deleted", "revoked", "archived")),
                )
            )
            if skill is None or skill.current_version_id is None:
                raise ValueError("selected expert Skill is unavailable for release")
            source_version_id = int(reference.get("version_id") or skill.current_version_id)
            source_version = await self.db.scalar(
                select(SkillVersion).where(
                    SkillVersion.id == source_version_id,
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.status != "deleted",
                )
            )
            if (
                source_version is None
                or not source_version.package_uri
                or not source_version.package_sha256
            ):
                raise ValueError("selected expert Skill version is unavailable for release")
            reference_source = str(reference.get("source") or "public")
            is_public_scope = (
                str(skill.asset_scope) == "public" and str(skill.visibility) != "private"
            )
            is_personal_owned = (
                str(skill.asset_scope) == "personal"
                and str(skill.visibility) == "private"
                and int(skill.owner_user_id) == int(package.owner_user_id)
            )
            if reference_source != "personal":
                if is_personal_owned:
                    # Legacy stored skill_refs may label a personal Skill owned by the
                    # package creator as source="public"; recover the attribution so the
                    # release preflight matches the save-time rule.
                    reference_source = "personal"
                elif not is_public_scope:
                    raise ValueError("selected expert Skill is not public")
                continue
            if not is_personal_owned:
                raise ValueError("selected personal expert Skill is unavailable for release")
        mcp_ids = {
            int(item["asset_id"])
            for item in capabilities["mcps"]
            if isinstance(item, dict) and item.get("asset_id") is not None
        }
        if mcp_ids:
            visible_mcps = set(
                await self.db.scalars(
                    select(Mcp.id).where(
                        Mcp.org_id == version.org_id,
                        Mcp.id.in_(mcp_ids),
                        Mcp.visibility != "private",
                        Mcp.status == "published",
                        Mcp.revoked_at.is_(None),
                        Mcp.current_version_id.is_not(None),
                    )
                )
            )
            if {int(item) for item in visible_mcps} != mcp_ids:
                raise ValueError("selected expert MCP is unavailable for release")
        knowledge_ids = {
            int(item["asset_id"])
            for item in capabilities["knowledge_bases"]
            if isinstance(item, dict) and item.get("asset_id") is not None
        }
        if knowledge_ids:
            available = set(
                await self.db.scalars(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.org_id == version.org_id,
                        KnowledgeBase.id.in_(knowledge_ids),
                        KnowledgeBase.status == "active",
                    )
                )
            )
            if {int(item) for item in available} != knowledge_ids:
                raise ValueError("selected expert knowledge base is unavailable for release")
        await self.db.flush()


    async def _assert_release_artifacts_ready(
        self,
        *,
        version: ExpertPackageVersion,
    ) -> None:
        await self._prepare_release_artifacts(version=version)


    async def _pending_review(
        self,
        *,
        org_id: int,
        review_id: int,
    ) -> TemplateReview:
        review = await self.db.scalar(
            select(TemplateReview).where(
                TemplateReview.org_id == org_id,
                TemplateReview.id == review_id,
                TemplateReview.status == "pending",
            )
        )
        if review is None:
            raise ExpertProfileError("expert_review_not_pending", status_code=409)
        if dict(review.review_snapshot or {}).get("schema_version") != "expert.review/v2":
            raise ExpertProfileError("expert_review_not_found", status_code=404)
        return review


    async def _unpublish_package(self, package: ExpertPackage) -> None:
        await self._hide_market_package(package)
        package.status = "active"
        package.visibility = "private"


    async def _hide_market_package(self, package: ExpertPackage) -> None:
        templates = (
            (
                await self.db.execute(
                    select(PlazaTemplate)
                    .join(
                        TemplateVersion,
                        TemplateVersion.template_id == PlazaTemplate.id,
                    )
                    .where(
                        PlazaTemplate.org_id == package.org_id,
                        TemplateVersion.source_expert_package_id == package.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for template in templates:
            template.status = "unpublished"
        await self.db.execute(
            delete(Permission).where(
                Permission.org_id == package.org_id,
                Permission.asset_type == "expert_package",
                Permission.asset_id == package.id,
            )
        )


__all__ = ["ExpertReviewCommandService"]
