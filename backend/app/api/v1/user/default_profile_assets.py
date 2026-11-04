from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.hermes_runtime.skill_visibility_policy import PLATFORM_DEFAULT_SKILL_NAMES
from app.database import get_db
from app.models import (
    Mcp,
    McpVersion,
    ProfileConvergenceJob,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    Skill,
    SkillVersion,
)
from app.schemas.profile_desired import (
    DefaultProfileAssetItem,
    DefaultProfileAssetMutation,
    DefaultProfileAssetOperationAccepted,
    DefaultProfileAssetView,
    UserAssetCatalogView,
)
from app.schemas.personal_skills import (
    PersonalSkillInstallAccepted,
    PersonalSkillSummary,
)
from app.services.personal_skill_assets import PersonalSkillAssetError
from app.services.personal_skill_categories import (
    PersonalSkillCategoryError,
    PersonalSkillCategoryService,
)
from app.services.personal_skill_installation import PersonalSkillInstallationService
from app.services.plaza.user_asset_scope import (
    DefaultUserAssetScope,
    UserAssetScopeError,
    resolve_default_user_asset_scope,
)
from app.services.profile_desired import ProfileDesiredError
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.runtime_workspace import Principal


router = APIRouter(tags=["user-default-profile-assets"])


async def _read_skill_upload(request: Request) -> tuple[bytes, str | None]:
    """Read multipart Skill uploads while accepting the legacy raw body."""
    content_type = str(getattr(request, "headers", {}).get("content-type", ""))
    if content_type.lower().startswith("multipart/form-data"):
        form = await request.form()
        file = form.get("file")
        if file is None or not hasattr(file, "read"):
            raise HTTPException(status_code=422, detail="file is required")
        description = form.get("description")
        return await file.read(), description if isinstance(description, str) else None

    headers = getattr(request, "headers", {})
    return await request.body(), headers.get("X-Skill-Description")


@router.get("/assets/catalog", response_model=UserAssetCatalogView)
async def get_user_asset_catalog(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> UserAssetCatalogView:
    skill_rows = (
        await db.execute(
            select(Skill, SkillVersion)
            .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
            .where(
                Skill.org_id == principal.org_id,
                Skill.asset_scope == "public",
                Skill.visibility != "private",
                Skill.status == "published",
                Skill.market_visible.is_(True),
                Skill.revoked_at.is_(None),
                func.lower(func.replace(Skill.slug, "_", "-")).notin_(
                    PLATFORM_DEFAULT_SKILL_NAMES
                ),
                func.lower(func.replace(Skill.name, "_", "-")).notin_(
                    PLATFORM_DEFAULT_SKILL_NAMES
                ),
            )
            .order_by(Skill.name, Skill.id)
        )
    ).all()
    mcp_rows = (
        await db.execute(
            select(Mcp, McpVersion)
            .join(McpVersion, McpVersion.id == Mcp.current_version_id)
            .where(
                Mcp.org_id == principal.org_id,
                Mcp.visibility != "private",
                Mcp.status == "published",
                Mcp.revoked_at.is_(None),
            )
            .order_by(Mcp.name, Mcp.id)
        )
    ).all()
    return UserAssetCatalogView(
        skills=[
            DefaultProfileAssetItem(
                asset_type="skill",
                asset_id=int(skill.id),
                current_version_id=int(version.id),
                asset_scope="public",
                version=int(version.version),
                configured=False,
                desired=False,
                applied=False,
                name=str(skill.name),
                description=skill.description,
                status=str(skill.status),
                category_key=str(skill.category_key),
            )
            for skill, version in skill_rows
        ],
        mcps=[
            DefaultProfileAssetItem(
                asset_type="mcp",
                asset_id=int(mcp.id),
                current_version_id=int(version.id),
                version=int(version.version),
                configured=False,
                desired=False,
                applied=False,
                name=str(mcp.name),
                description=mcp.description,
                status=str(mcp.status),
                transport=str(version.transport),
            )
            for mcp, version in mcp_rows
        ],
    )


async def _scope(
    *, db: AsyncSession, principal: Principal, runtime_workspace_id: int
) -> DefaultUserAssetScope:
    try:
        resolved = await resolve_default_user_asset_scope(
            db,
            org_id=principal.org_id,
            user_id=principal.user_id,
        )
    except UserAssetScopeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc
    if resolved.runtime_workspace_id != runtime_workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="runtime_workspace_not_found")
    return resolved


async def _applied_asset_ids(
    db: AsyncSession,
    *,
    runtime_agent_profile_id: int,
    applied_revision: int,
) -> tuple[set[int], set[int]]:
    if applied_revision <= 0:
        return set(), set()
    revision = await db.scalar(
        select(RuntimeAgentProfileRevision).where(
            RuntimeAgentProfileRevision.runtime_agent_profile_id
            == runtime_agent_profile_id,
            RuntimeAgentProfileRevision.revision == applied_revision,
        )
    )
    if revision is None:
        return set(), set()
    snapshot = dict(revision.snapshot or {})
    return (
        {
            int(item["skill_id"])
            for item in snapshot.get("skills", [])
            if isinstance(item, dict) and item.get("skill_id") is not None
        },
        {
            int(item["mcp_id"])
            for item in snapshot.get("mcps", [])
            if isinstance(item, dict) and item.get("mcp_id") is not None
        },
    )


@router.get(
    "/runtime-workspaces/{runtime_workspace_id}/default-profile/assets",
    response_model=DefaultProfileAssetView,
)
async def get_default_profile_assets(
    runtime_workspace_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> DefaultProfileAssetView:
    scope = await _scope(
        db=db,
        principal=principal,
        runtime_workspace_id=runtime_workspace_id,
    )
    profile_row = await db.get(RuntimeAgentProfile, scope.runtime_agent_profile_id)
    if profile_row is None:
        raise HTTPException(status_code=404, detail="default_profile_not_found")

    desired_skill_ids = set(
        await db.scalars(
            select(ProfileSkillBinding.skill_id).where(
                ProfileSkillBinding.runtime_agent_profile_id
                == scope.runtime_agent_profile_id
            )
        )
    )
    desired_mcp_ids = set(
        await db.scalars(
            select(ProfileMcpBinding.mcp_id).where(
                ProfileMcpBinding.runtime_agent_profile_id
                == scope.runtime_agent_profile_id
            )
        )
    )
    applied_skill_ids, applied_mcp_ids = await _applied_asset_ids(
        db,
        runtime_agent_profile_id=scope.runtime_agent_profile_id,
        applied_revision=int(profile_row.applied_revision or 0),
    )
    convergence_job = await db.scalar(
        select(ProfileConvergenceJob).where(
            ProfileConvergenceJob.runtime_agent_profile_id
            == scope.runtime_agent_profile_id
        )
    )
    skill_rows = (
        await db.execute(
            select(Skill, SkillVersion)
            .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
            .where(
                Skill.org_id == principal.org_id,
                or_(
                    (
                        (Skill.asset_scope == "public")
                        & (Skill.visibility != "private")
                        & func.lower(func.replace(Skill.slug, "_", "-")).notin_(
                            PLATFORM_DEFAULT_SKILL_NAMES
                        )
                        & func.lower(func.replace(Skill.name, "_", "-")).notin_(
                            PLATFORM_DEFAULT_SKILL_NAMES
                        )
                        & or_(
                            (
                                (Skill.status == "published")
                                & (Skill.revoked_at.is_(None))
                                & (Skill.market_visible.is_(True))
                            ),
                            Skill.id.in_(desired_skill_ids),
                        )
                    ),
                    (
                        (Skill.asset_scope == "personal")
                        & (Skill.owner_user_id == principal.user_id)
                        & or_(
                            Skill.status.in_(("published", "private")),
                            Skill.id.in_(desired_skill_ids),
                            Skill.id.in_(applied_skill_ids),
                        )
                    ),
                ),
            )
            .order_by(Skill.name.asc(), Skill.id.asc())
        )
    ).all()
    mcp_rows = (
        await db.execute(
            select(Mcp, McpVersion)
            .join(McpVersion, McpVersion.id == Mcp.current_version_id)
            .where(
                Mcp.org_id == principal.org_id,
                Mcp.visibility != "private",
                or_(
                    (
                        (Mcp.status == "published")
                        & (Mcp.revoked_at.is_(None))
                    ),
                    Mcp.id.in_(desired_mcp_ids),
                ),
            )
            .order_by(Mcp.name.asc(), Mcp.id.asc())
        )
    ).all()
    return DefaultProfileAssetView(
        runtime_workspace_id=runtime_workspace_id,
        runtime_agent_profile_id=scope.runtime_agent_profile_id,
        desired_revision=int(profile_row.desired_revision or 0),
        applied_revision=int(profile_row.applied_revision or 0),
        convergence_status=(
            "applied"
            if int(profile_row.desired_revision or 0)
            == int(profile_row.applied_revision or 0)
            else (
                str(convergence_job.status)
                if convergence_job is not None
                else "waiting"
            )
        ),
        last_error=(
            None
            if int(profile_row.desired_revision or 0)
            == int(profile_row.applied_revision or 0)
            else (
                convergence_job.last_error_message
                or convergence_job.last_error_code
                if convergence_job is not None
                else profile_row.last_error
            )
        ),
        skills=[
            DefaultProfileAssetItem(
                asset_type="skill",
                asset_id=int(asset.id),
                current_version_id=int(version.id),
                asset_scope=str(asset.asset_scope),
                version=int(version.version),
                configured=int(asset.id) in desired_skill_ids,
                desired=(int(asset.id) in desired_skill_ids and asset.revoked_at is None),
                applied=int(asset.id) in applied_skill_ids,
                name=str(asset.name),
                description=asset.description,
                status=("force_disabled" if asset.revoked_at else str(asset.status)),
                category_key=str(asset.category_key),
            )
            for asset, version in skill_rows
        ],
        mcps=[
            DefaultProfileAssetItem(
                asset_type="mcp",
                asset_id=int(asset.id),
                current_version_id=int(version.id),
                version=int(version.version),
                configured=int(asset.id) in desired_mcp_ids,
                desired=(int(asset.id) in desired_mcp_ids and asset.revoked_at is None),
                applied=int(asset.id) in applied_mcp_ids,
                name=str(asset.name),
                description=asset.description,
                status=("force_disabled" if asset.revoked_at else str(asset.status)),
                transport=str(version.transport),
            )
            for asset, version in mcp_rows
        ],
    )


async def _mutate(
    *,
    runtime_workspace_id: int,
    asset_type: str,
    asset_id: int,
    action: str,
    principal: Principal,
    db: AsyncSession,
) -> DefaultProfileAssetOperationAccepted:
    scope = await _scope(
        db=db,
        principal=principal,
        runtime_workspace_id=runtime_workspace_id,
    )
    operation_id = f"asset-{uuid4().hex}"
    try:
        result = await ProfileDesiredMutationService(db).mutate_default_asset(
            DefaultProfileAssetMutation(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=scope.runtime_agent_profile_id,
                asset_type=asset_type,
                action=action,
                asset_id=asset_id,
                operation_id=operation_id,
            )
        )
        await db.commit()
    except ProfileDesiredError as exc:
        await db.rollback()
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code.endswith("UNAVAILABLE")
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=exc.code) from exc
    return DefaultProfileAssetOperationAccepted(
        operation_id=operation_id,
        runtime_workspace_id=runtime_workspace_id,
        runtime_agent_profile_id=scope.runtime_agent_profile_id,
        asset_type=asset_type,
        asset_id=asset_id,
        action=action,
        desired_revision=result.desired_revision,
        applied_revision=result.applied_revision,
        convergence_status=result.convergence_status,
    )


@router.post(
    "/runtime-workspaces/{runtime_workspace_id}/default-profile/personal-skills",
    response_model=PersonalSkillInstallAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_and_install_personal_skill(
    runtime_workspace_id: int,
    request: Request,
    category_key: str = "uncategorized",
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> PersonalSkillInstallAccepted:
    scope = await _scope(
        db=db,
        principal=principal,
        runtime_workspace_id=runtime_workspace_id,
    )
    content, skill_description = await _read_skill_upload(request)
    try:
        category_key, _category_scope = await PersonalSkillCategoryService(
            db
        ).validate_upload_category(
            principal=principal,
            category_key=category_key,
        )
        result = await PersonalSkillInstallationService(
            db,
            root=settings.HERMES_PUBLIC_ASSET_ROOT,
        ).upload_and_install(
            principal=principal,
            scope=scope,
            content=content,
            category_key=category_key,
            description=skill_description,
        )
    except (PersonalSkillAssetError, PersonalSkillCategoryError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ProfileDesiredError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code.endswith("UNAVAILABLE")
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=exc.code) from exc
    asset = result.asset
    return PersonalSkillInstallAccepted(
        asset=PersonalSkillSummary(
            skill_id=asset.skill_id,
            skill_version_id=asset.skill_version_id,
            slug=asset.slug,
            version=asset.version,
            name=asset.name,
            description=asset.description,
            category_key=asset.category_key,
            category_scope=_category_scope,
            status=asset.status,
        ),
        operation=DefaultProfileAssetOperationAccepted(
            operation_id=result.operation_id,
            runtime_workspace_id=scope.runtime_workspace_id,
            runtime_agent_profile_id=scope.runtime_agent_profile_id,
            asset_type="skill",
            asset_id=asset.skill_id,
            action="install",
            desired_revision=result.desired.desired_revision,
            applied_revision=result.desired.applied_revision,
            convergence_status=result.desired.convergence_status,
        ),
    )


@router.put(
    "/runtime-workspaces/{runtime_workspace_id}/default-profile/skills/{skill_id}",
    response_model=DefaultProfileAssetOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def install_default_profile_skill(
    runtime_workspace_id: int,
    skill_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> DefaultProfileAssetOperationAccepted:
    return await _mutate(
        runtime_workspace_id=runtime_workspace_id,
        asset_type="skill",
        asset_id=skill_id,
        action="install",
        principal=principal,
        db=db,
    )


@router.delete(
    "/runtime-workspaces/{runtime_workspace_id}/default-profile/skills/{skill_id}",
    response_model=DefaultProfileAssetOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def uninstall_default_profile_skill(
    runtime_workspace_id: int,
    skill_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> DefaultProfileAssetOperationAccepted:
    return await _mutate(
        runtime_workspace_id=runtime_workspace_id,
        asset_type="skill",
        asset_id=skill_id,
        action="uninstall",
        principal=principal,
        db=db,
    )


@router.put(
    "/runtime-workspaces/{runtime_workspace_id}/default-profile/mcps/{mcp_id}",
    response_model=DefaultProfileAssetOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def install_default_profile_mcp(
    runtime_workspace_id: int,
    mcp_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> DefaultProfileAssetOperationAccepted:
    return await _mutate(
        runtime_workspace_id=runtime_workspace_id,
        asset_type="mcp",
        asset_id=mcp_id,
        action="install",
        principal=principal,
        db=db,
    )


@router.delete(
    "/runtime-workspaces/{runtime_workspace_id}/default-profile/mcps/{mcp_id}",
    response_model=DefaultProfileAssetOperationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def uninstall_default_profile_mcp(
    runtime_workspace_id: int,
    mcp_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> DefaultProfileAssetOperationAccepted:
    return await _mutate(
        runtime_workspace_id=runtime_workspace_id,
        asset_type="mcp",
        asset_id=mcp_id,
        action="uninstall",
        principal=principal,
        db=db,
    )
