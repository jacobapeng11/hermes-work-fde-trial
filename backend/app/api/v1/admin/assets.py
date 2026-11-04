from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.config import settings
from app.database import get_db
from app.models import Mcp, McpVersion, Skill, SkillCategory, SkillVersion
from app.schemas.asset_catalog import (
    AssetExecutionSecurityRequest,
    CreateMcpAssetCommand,
    CreateSkillAssetCommand,
    CreateStableMcpRequest,
    PublishMcpVersionCommand,
    PublishSkillVersionCommand,
    PublishStableMcpVersionRequest,
    SkillVersionMaterial,
    StableAssetSummary,
    StableAssetVersionSummary,
    StableMcpPublicationResponse,
)
from app.schemas.assets import CreateSkillCategoryRequest, SkillCategorySummary
from app.schemas.asset_rollout import AssetRolloutCommand, AssetRolloutResult
from app.services.admin_auth import AdminPrincipal
from app.services.asset_catalog import AssetCatalogError, AssetCatalogService
from app.services.asset_publication import PublicAssetPublicationService
from app.services.asset_rollout import AssetRolloutService
from app.services.plaza.public_catalog.package_parser import (
    SkillPackageError,
    normalize_skill_name,
    parse_skill_metadata,
    runtime_slug_from_normalized_name,
    validate_category_key,
)
from app.services.plaza.public_catalog.package_storage import PublicSkillPackageStorage


router = APIRouter(prefix="/assets", tags=["admin-assets"])


def _raise_catalog(exc: AssetCatalogError) -> None:
    code = (
        status.HTTP_404_NOT_FOUND if exc.code.endswith("UNAVAILABLE") else status.HTTP_409_CONFLICT
    )
    raise HTTPException(status_code=code, detail=exc.code) from exc


def _security_rollout_id(
    *, asset_type: str, asset_id: int, action: str, idempotency_key: str
) -> str:
    digest = sha256(
        f"{asset_type}:{asset_id}:{action}:{idempotency_key}".encode()
    ).hexdigest()[:32]
    return f"{asset_type}-{action}-{asset_id}-{digest}"


def _skill_summary(skill: Skill, version: SkillVersion | None) -> StableAssetSummary:
    return StableAssetSummary(
        asset_type="skill",
        asset_id=int(skill.id),
        current_version_id=int(skill.current_version_id) if skill.current_version_id else None,
        version=int(version.version) if version else None,
        slug=str(skill.slug),
        name=str(skill.name),
        description=skill.description,
        status=str(skill.status),
        market_visible=bool(skill.market_visible),
        execution_status=("force_disabled" if skill.revoked_at else "enabled"),
        visibility=str(skill.visibility),
        category_key=str(skill.category_key),
        manifest=dict(version.manifest or {}) if version else {},
    )


def _mcp_summary(mcp: Mcp, version: McpVersion | None) -> StableAssetSummary:
    return StableAssetSummary(
        asset_type="mcp",
        asset_id=int(mcp.id),
        current_version_id=int(mcp.current_version_id) if mcp.current_version_id else None,
        version=int(version.version) if version else None,
        slug=str(mcp.slug),
        name=str(mcp.name),
        description=mcp.description,
        status=str(mcp.status),
        execution_status=("force_disabled" if mcp.revoked_at else "enabled"),
        visibility=str(mcp.visibility),
        transport=str(version.transport) if version else str(mcp.transport),
        connection_config=dict(version.connection_config or {}) if version else {},
        tools_schema=list(version.tools_schema) if version and version.tools_schema else None,
    )


@router.get("/skill-categories", response_model=list[SkillCategorySummary])
async def list_skill_categories(
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[SkillCategorySummary]:
    rows = await db.scalars(
        select(SkillCategory)
        .where(SkillCategory.org_id == principal.org_id)
        .order_by(SkillCategory.display_name, SkillCategory.category_key)
    )
    return [
        SkillCategorySummary(
            category_key=row.category_key,
            display_name=row.display_name,
            is_builtin=bool(row.is_builtin),
            is_market_visible=bool(row.is_market_visible),
            is_personal_visible=bool(row.is_personal_visible),
        )
        for row in rows
    ]


@router.post("/skill-categories", response_model=SkillCategorySummary, status_code=201)
async def create_skill_category(
    payload: CreateSkillCategoryRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> SkillCategorySummary:
    display_name = " ".join(payload.display_name.split()).strip()
    if not display_name or len(display_name) > 100:
        raise HTTPException(status_code=400, detail="category_display_name_invalid")
    category_key = payload.category_key or (
        f"category-{sha256(display_name.casefold().encode('utf-8')).hexdigest()[:16]}"
    )
    try:
        category_key = validate_category_key(category_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="category_key_invalid") from exc
    if category_key == "personal":
        raise HTTPException(status_code=400, detail="personal category is built-in")
    existing = await db.scalar(
        select(SkillCategory).where(
            SkillCategory.org_id == principal.org_id,
            (SkillCategory.category_key == category_key)
            | (func.lower(SkillCategory.display_name) == display_name.casefold()),
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="category_exists")
    row = SkillCategory(
        org_id=principal.org_id,
        category_key=category_key,
        display_name=display_name,
        is_builtin=False,
        is_market_visible=True,
        is_personal_visible=True,
    )
    db.add(row)
    await db.commit()
    return SkillCategorySummary(
        category_key=row.category_key,
        display_name=row.display_name,
        is_builtin=False,
        is_market_visible=True,
        is_personal_visible=True,
    )


@router.get("/skills", response_model=list[StableAssetSummary])
async def list_skills(
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[StableAssetSummary]:
    rows = await db.execute(
        select(Skill, SkillVersion)
        .outerjoin(SkillVersion, SkillVersion.id == Skill.current_version_id)
        .where(
            Skill.org_id == principal.org_id,
            Skill.asset_scope == "public",
            Skill.status != "deleted",
        )
        .order_by(Skill.created_at.desc(), Skill.id.desc())
    )
    return [_skill_summary(skill, version) for skill, version in rows.all()]


@router.get("/skills/{skill_id}/versions", response_model=list[StableAssetVersionSummary])
async def list_skill_versions(
    skill_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[StableAssetVersionSummary]:
    skill = await db.scalar(
        select(Skill).where(
            Skill.id == skill_id,
            Skill.org_id == principal.org_id,
            Skill.asset_scope == "public",
            Skill.status != "deleted",
        )
    )
    if skill is None:
        raise HTTPException(status_code=404, detail="SKILL_UNAVAILABLE")
    versions = await db.scalars(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.version.desc())
    )
    return [
        StableAssetVersionSummary(
            asset_type="skill",
            asset_id=skill_id,
            version_id=int(item.id),
            version=int(item.version),
            status=str(item.status),
            content_digest=f"sha256:{item.package_sha256}",
            manifest=dict(item.manifest or {}),
        )
        for item in versions
    ]


async def _store_skill_package(
    *,
    db: AsyncSession,
    org_id: int,
    skill_id: int | None,
    content: bytes,
    category_key: str,
) -> tuple[object, dict, str, str]:
    try:
        category = validate_category_key(category_key)
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    visible = await db.scalar(
        select(SkillCategory.id).where(
            SkillCategory.org_id == org_id,
            SkillCategory.category_key == category,
            SkillCategory.is_market_visible.is_(True),
        )
    )
    if visible is None:
        raise HTTPException(status_code=400, detail="category_not_market_visible")
    try:
        metadata = parse_skill_metadata(content)
        name = str(metadata["name"])
        slug = runtime_slug_from_normalized_name(normalize_skill_name(name))
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    next_version = 1
    if skill_id is not None:
        next_version = (
            int(
                await db.scalar(
                    select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == skill_id)
                )
                or 0
            )
            + 1
        )
    try:
        package = PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).store(
            org_id=org_id,
            # Public market storage is keyed by the stable catalog slug, not
            # by version. Version rows remain audit-only metadata.
            normalized_name=slug,
            version_number=next_version,
            content=content,
            category_key=category,
        )
    except SkillPackageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return package, metadata, category, slug


@router.post("/skills", response_model=StableAssetSummary, status_code=201)
async def create_skill(
    category_key: str = Query(...),
    file: UploadFile = File(...),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    package, metadata, category, slug = await _store_skill_package(
        db=db,
        org_id=principal.org_id,
        skill_id=None,
        content=await file.read(),
        category_key=category_key,
    )
    try:
        result = await AssetCatalogService(db).create_skill(
            CreateSkillAssetCommand(
                org_id=principal.org_id,
                owner_user_id=principal.user_id,
                slug=slug,
                name=str(metadata["name"]),
                description=metadata.get("description"),
                asset_scope="public",
                visibility="organization",
                material=SkillVersionMaterial(
                    package_uri=str(package.deployment_package_uri),
                    package_sha256=str(package.deployment_sha256),
                    manifest=dict(package.manifest),
                ),
            )
        )
        skill = await db.get(Skill, result.asset_id)
        version = await db.get(SkillVersion, result.version_id)
        assert skill is not None and version is not None
        skill.category_key = category
        await db.commit()
        PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).finalize_package(package)
        return _skill_summary(skill, version)
    except AssetCatalogError as exc:
        await db.rollback()
        PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).discard_package(package)
        _raise_catalog(exc)
    except Exception:
        await db.rollback()
        PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).discard_package(package)
        raise


@router.post("/skills/{skill_id}/versions", response_model=StableAssetSummary, status_code=201)
async def publish_skill_version(
    skill_id: int,
    category_key: str = Query(...),
    file: UploadFile = File(...),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    skill = await db.scalar(
        select(Skill).where(
            Skill.id == skill_id,
            Skill.org_id == principal.org_id,
            Skill.asset_scope == "public",
            Skill.status != "deleted",
        )
    )
    if skill is None:
        raise HTTPException(status_code=404, detail="SKILL_UNAVAILABLE")
    package, metadata, category, _ = await _store_skill_package(
        db=db,
        org_id=principal.org_id,
        skill_id=skill_id,
        content=await file.read(),
        category_key=category_key,
    )
    if normalize_skill_name(str(metadata["name"])) != normalize_skill_name(str(skill.name)):
        PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).discard_package(package)
        raise HTTPException(status_code=409, detail="skill_name_mismatch")
    try:
        publication = await PublicAssetPublicationService(db).publish_skill(
            command=PublishSkillVersionCommand(
                org_id=principal.org_id,
                skill_id=skill_id,
                material=SkillVersionMaterial(
                    package_uri=str(package.deployment_package_uri),
                    package_sha256=str(package.deployment_sha256),
                    manifest=dict(package.manifest),
                ),
            ),
            rollout_id=f"skill-{skill_id}-{uuid4().hex}",
            created_by_user_id=principal.user_id,
        )
        assert publication.catalog is not None
        skill.category_key = category
        version = await db.get(SkillVersion, publication.catalog.version_id)
        await db.commit()
        PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).finalize_package(package)
        assert version is not None
        return _skill_summary(skill, version)
    except AssetCatalogError as exc:
        await db.rollback()
        PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).discard_package(package)
        _raise_catalog(exc)
    except Exception:
        await db.rollback()
        PublicSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).discard_package(package)
        raise


@router.post(
    "/skills/{skill_id}/versions/{version_id}/activate",
    response_model=StableAssetSummary,
)
async def activate_skill_version(
    skill_id: int,
    version_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="SKILL_VERSION_RESTORE_DISABLED",
    )


@router.post("/skills/{skill_id}/unpublish", response_model=StableAssetSummary)
async def unpublish_skill(
    skill_id: int,
    payload: AssetExecutionSecurityRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    del payload
    try:
        skill = await AssetCatalogService(db).set_skill_market_visible(
            org_id=principal.org_id, skill_id=skill_id, visible=False
        )
        version = await db.get(SkillVersion, skill.current_version_id) if skill.current_version_id else None
        await db.commit()
    except AssetCatalogError as exc:
        await db.rollback()
        _raise_catalog(exc)
    return _skill_summary(skill, version)


@router.post("/skills/{skill_id}/publish", response_model=StableAssetSummary)
async def republish_skill(
    skill_id: int,
    payload: AssetExecutionSecurityRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    try:
        publication = await PublicAssetPublicationService(db).republish_skill(
            org_id=principal.org_id,
            skill_id=skill_id,
            rollout_id=_security_rollout_id(
                asset_type="skill",
                asset_id=skill_id,
                action="publish",
                idempotency_key=idempotency_key,
            ),
            created_by_user_id=principal.user_id,
            change_summary=payload.reason,
        )
        await db.commit()
    except AssetCatalogError as exc:
        await db.rollback()
        _raise_catalog(exc)
    assert publication.catalog is not None
    skill = await db.get(Skill, skill_id)
    version = await db.get(SkillVersion, publication.catalog.version_id)
    assert skill is not None
    return _skill_summary(skill, version)


@router.post(
    "/skills/{skill_id}/force-disable",
    response_model=StableAssetSummary,
)
async def force_disable_skill(
    skill_id: int,
    payload: AssetExecutionSecurityRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    del idempotency_key
    return await unpublish_skill(skill_id, payload, principal, db)


@router.post(
    "/skills/{skill_id}/enable",
    response_model=StableAssetSummary,
)
async def enable_skill(
    skill_id: int,
    payload: AssetExecutionSecurityRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    return await republish_skill(skill_id, payload, idempotency_key, principal, db)


@router.delete(
    "/skills/{skill_id}",
    response_model=AssetRolloutResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_skill(
    skill_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutResult:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="SKILL_PERMANENT_DELETE_DISABLED",
    )


@router.patch("/skills/{skill_id}/category", response_model=StableAssetSummary)
async def update_skill_category(
    skill_id: int,
    category_key: str = Query(...),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    category = validate_category_key(category_key)
    visible = await db.scalar(
        select(SkillCategory.id).where(
            SkillCategory.org_id == principal.org_id,
            SkillCategory.category_key == category,
            SkillCategory.is_market_visible.is_(True),
        )
    )
    row = await db.execute(
        select(Skill, SkillVersion)
        .outerjoin(SkillVersion, SkillVersion.id == Skill.current_version_id)
        .where(
            Skill.id == skill_id,
            Skill.org_id == principal.org_id,
            Skill.asset_scope == "public",
            Skill.status != "deleted",
        )
    )
    item = row.first()
    if item is None:
        raise HTTPException(status_code=404, detail="SKILL_UNAVAILABLE")
    if visible is None:
        raise HTTPException(status_code=400, detail="category_not_market_visible")
    skill, version = item
    skill.category_key = category
    await db.commit()
    return _skill_summary(skill, version)


@router.get("/mcps", response_model=list[StableAssetSummary])
async def list_mcps(
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[StableAssetSummary]:
    rows = await db.execute(
        select(Mcp, McpVersion)
        .outerjoin(McpVersion, McpVersion.id == Mcp.current_version_id)
        .where(
            Mcp.org_id == principal.org_id,
            Mcp.visibility != "private",
            Mcp.status != "deleted",
        )
        .order_by(Mcp.created_at.desc(), Mcp.id.desc())
    )
    return [_mcp_summary(mcp, version) for mcp, version in rows.all()]


@router.post("/mcps", response_model=StableAssetSummary, status_code=201)
async def create_mcp(
    payload: CreateStableMcpRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableAssetSummary:
    try:
        result = await AssetCatalogService(db).create_mcp(
            CreateMcpAssetCommand(
                org_id=principal.org_id,
                owner_user_id=principal.user_id,
                slug=payload.slug,
                name=payload.name,
                description=payload.description,
                visibility=payload.visibility,
                material=payload.material,
            )
        )
        mcp = await db.get(Mcp, result.asset_id)
        version = await db.get(McpVersion, result.version_id)
        await db.commit()
        assert mcp is not None and version is not None
        return _mcp_summary(mcp, version)
    except AssetCatalogError as exc:
        await db.rollback()
        _raise_catalog(exc)


@router.post(
    "/mcps/{mcp_id}/versions",
    response_model=StableMcpPublicationResponse,
    status_code=201,
)
async def publish_mcp_version(
    mcp_id: int,
    payload: PublishStableMcpVersionRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> StableMcpPublicationResponse:
    try:
        publication = await PublicAssetPublicationService(db).publish_mcp(
            command=PublishMcpVersionCommand(
                org_id=principal.org_id,
                mcp_id=mcp_id,
                expected_current_version_id=payload.expected_current_version_id,
                name=payload.name,
                description=payload.description,
                material=payload.material,
            ),
            rollout_id=f"mcp-{mcp_id}-{uuid4().hex}",
            created_by_user_id=principal.user_id,
            change_summary=payload.change_summary,
        )
        assert publication.catalog is not None
        mcp = await db.get(Mcp, mcp_id)
        version = await db.get(McpVersion, publication.catalog.version_id)
        await db.commit()
        assert mcp is not None and version is not None
        return StableMcpPublicationResponse(
            asset=_mcp_summary(mcp, version),
            rollout=publication.rollout,
        )
    except AssetCatalogError as exc:
        await db.rollback()
        _raise_catalog(exc)


async def _set_mcp_execution(
    *,
    mcp_id: int,
    enabled: bool,
    payload: AssetExecutionSecurityRequest,
    idempotency_key: str,
    principal: AdminPrincipal,
    db: AsyncSession,
) -> AssetRolloutResult:
    try:
        publication = await PublicAssetPublicationService(db).set_mcp_execution_enabled(
            org_id=principal.org_id,
            mcp_id=mcp_id,
            enabled=enabled,
            rollout_id=_security_rollout_id(
                asset_type="mcp",
                asset_id=mcp_id,
                action="enable" if enabled else "revoke",
                idempotency_key=idempotency_key,
            ),
            created_by_user_id=principal.user_id,
            reason=payload.reason,
        )
        await db.commit()
    except AssetCatalogError as exc:
        await db.rollback()
        _raise_catalog(exc)
    assert publication.rollout is not None
    return publication.rollout


@router.post(
    "/mcps/{mcp_id}/force-disable",
    response_model=AssetRolloutResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def force_disable_mcp(
    mcp_id: int,
    payload: AssetExecutionSecurityRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutResult:
    return await _set_mcp_execution(
        mcp_id=mcp_id,
        enabled=False,
        payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db=db,
    )


@router.post(
    "/mcps/{mcp_id}/enable",
    response_model=AssetRolloutResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enable_mcp(
    mcp_id: int,
    payload: AssetExecutionSecurityRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutResult:
    return await _set_mcp_execution(
        mcp_id=mcp_id,
        enabled=True,
        payload=payload,
        idempotency_key=idempotency_key,
        principal=principal,
        db=db,
    )


@router.delete(
    "/mcps/{mcp_id}",
    response_model=AssetRolloutResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_mcp(
    mcp_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutResult:
    try:
        publication = await PublicAssetPublicationService(
            db
        ).permanently_delete_mcp(
            org_id=principal.org_id,
            mcp_id=mcp_id,
            created_by_user_id=principal.user_id,
        )
        await db.commit()
    except AssetCatalogError as exc:
        await db.rollback()
        _raise_catalog(exc)
    assert publication.rollout is not None
    return publication.rollout
