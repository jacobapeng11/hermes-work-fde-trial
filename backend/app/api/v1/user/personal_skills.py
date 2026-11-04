from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.schemas.personal_skills import (
    PersonalSkillDetail,
    CreatePersonalSkillCategoryRequest,
    PersonalSkillCategorySummary,
    PersonalSkillRemovalResponse,
    PersonalSkillSummary,
    SkillDescriptionUpdateRequest,
    SystemSkillSummary,
)
from app.hermes_runtime.skill_visibility_policy import PLATFORM_DEFAULT_SKILL_NAMES
from app.hermes_runtime.system_skill_catalog import system_skill_descriptions
from app.services.asset_catalog import AssetCatalogError
from app.services.personal_skill_assets import (
    PersonalSkillAssetError,
    PersonalSkillAssetRecord,
    PersonalSkillAssetService,
    PersonalSkillPackageStorage,
    SqlAlchemyPersonalSkillAssetStore,
)
from app.services.personal_skill_lifecycle import PersonalSkillLifecycleService
from app.services.personal_skill_categories import (
    PersonalSkillCategoryError,
    PersonalSkillCategoryService,
)
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/personal-skills", tags=["user-personal-skills"])


async def _read_skill_upload(request: Request) -> tuple[bytes, str | None]:
    """Read the current multipart contract, retaining raw-body compatibility.

    Older clients sent the ZIP as the complete body and (optionally) the
    description in ``X-Skill-Description``.  New clients send both values as
    multipart fields so Unicode descriptions never need to pass through HTTP
    header validation.
    """
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


def _service(db: AsyncSession) -> PersonalSkillAssetService:
    return PersonalSkillAssetService(
        store=SqlAlchemyPersonalSkillAssetStore(db),
        root=settings.HERMES_PUBLIC_ASSET_ROOT,
    )


def _summary(record: PersonalSkillAssetRecord) -> PersonalSkillSummary:
    category_key = (
        "uncategorized"
        if record.category_key in {"other", "personal", "uncategorized"}
        else record.category_key
    )
    source = "hermes_managed" if record.manifest.get("kind") == "hermes_managed" else "personal_asset"
    return PersonalSkillSummary(
        skill_id=record.skill_id,
        skill_version_id=record.skill_version_id,
        slug=record.slug,
        version=record.version,
        name=record.name,
        description=record.description,
        category_key=category_key,
        category_scope=(
            "uncategorized"
            if category_key == "uncategorized"
            else "personal"
            if category_key.startswith("personal-")
            else "organization"
        ),
        status=record.status,
        source=source,
    )


@router.get("", response_model=list[PersonalSkillSummary])
async def list_personal_skills(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[PersonalSkillSummary]:
    records = await _service(db).list_skills(principal=principal)
    return [_summary(record) for record in records]


@router.get("/categories", response_model=list[PersonalSkillCategorySummary])
async def list_personal_skill_categories(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[PersonalSkillCategorySummary]:
    return await PersonalSkillCategoryService(db).list_available(principal=principal)


@router.post(
    "/categories",
    response_model=PersonalSkillCategorySummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_personal_skill_category(
    payload: CreatePersonalSkillCategoryRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> PersonalSkillCategorySummary:
    try:
        result = await PersonalSkillCategoryService(db).create(
            principal=principal,
            display_name=payload.display_name,
        )
        await db.commit()
    except PersonalSkillCategoryError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@router.get("/system", response_model=list[SystemSkillSummary])
async def list_system_skills(
    _principal: Principal = Depends(get_principal),
) -> list[SystemSkillSummary]:
    descriptions = system_skill_descriptions()
    return [
        SystemSkillSummary(
            key=name,
            name=name,
            description=descriptions.get(name),
        )
        for name in sorted(PLATFORM_DEFAULT_SKILL_NAMES)
    ]


@router.post("", response_model=PersonalSkillSummary, status_code=201)
async def upload_personal_skill(
    request: Request,
    category_key: str = "uncategorized",
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> PersonalSkillSummary:
    content, skill_description = await _read_skill_upload(request)
    try:
        category_key, _scope = await PersonalSkillCategoryService(
            db
        ).validate_upload_category(
            principal=principal,
            category_key=category_key,
        )
        record = await _service(db).upload_skill_zip(
            principal=principal,
            content=content,
            category_key=category_key,
            description=skill_description,
        )
        await db.commit()
    except (PersonalSkillAssetError, PersonalSkillCategoryError) as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(record)


@router.put("/{skill_id}", response_model=PersonalSkillSummary)
async def update_personal_skill_description(
    skill_id: int,
    payload: SkillDescriptionUpdateRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> PersonalSkillSummary:
    try:
        record = await _service(db).update_description(
            principal=principal,
            skill_id=skill_id,
            description=payload.description,
            name=payload.name,
        )
    except PersonalSkillAssetError as exc:
        await db.rollback()
        code = str(exc)
        status_code = 400 if code == "skill_description_too_long" else 409 if code.endswith("name_conflict") else 404
        raise HTTPException(status_code=status_code, detail=code) from exc
    return _summary(record)


@router.get("/{skill_id}", response_model=PersonalSkillDetail)
async def get_personal_skill(
    skill_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> PersonalSkillDetail:
    try:
        record = await _service(db).get_skill(principal=principal, skill_id=skill_id)
    except PersonalSkillAssetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PersonalSkillDetail(asset=_summary(record), detail=record.manifest)


@router.post("/{skill_id}/archive", response_model=PersonalSkillSummary)
async def archive_personal_skill(
    skill_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> PersonalSkillSummary:
    try:
        await PersonalSkillLifecycleService(db).archive(
            org_id=principal.org_id,
            user_id=principal.user_id,
            skill_id=skill_id,
        )
        await db.commit()
    except AssetCatalogError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    record = await _service(db).get_skill(principal=principal, skill_id=skill_id)
    return _summary(record)


@router.delete(
    "/{skill_id}",
    response_model=PersonalSkillRemovalResponse,
)
async def delete_personal_skill(
    skill_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> PersonalSkillRemovalResponse:
    try:
        result = await PersonalSkillLifecycleService(
            db
        ).delete(
            org_id=principal.org_id,
            user_id=principal.user_id,
            skill_id=skill_id,
            operation_id=f"personal-skill-remove:{uuid4().hex}",
        )
        await db.commit()
        # Database history and bindings are retained; the owner-requested
        # deletion only clears all current and legacy package files after it
        # is durable in MySQL.
        PersonalSkillPackageStorage(settings.HERMES_PUBLIC_ASSET_ROOT).remove_all(
            org_id=principal.org_id,
            user_id=principal.user_id,
            slug=result.storage_slug,
        )
    except AssetCatalogError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return PersonalSkillRemovalResponse(
        skill_id=result.skill_id,
        rollout_id=result.rollout_id,
        affected_profile_ids=list(result.affected_profile_ids),
        desired_revisions={
            item.runtime_agent_profile_id: item.desired_revision
            for item in result.desired_results
        },
    )
