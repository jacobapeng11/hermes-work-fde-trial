from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.api.v1.admin.deps import get_admin_principal
from app.config import settings
from app.database import get_db
from app.schemas.admin import (
    AdminModelCatalogPublishRequest,
    AdminModelCatalogPublishResponse,
    AdminModelCatalogReleaseSummary,
    AdminModelConfigEntrySummary,
    AdminModelConfigResponse,
    AdminModelVisibilityScope,
    AdminModelVisibilityUserSummary,
)
from app.schemas.user_directory import OrganizationDirectoryNode
from app.services.admin_audit import AdminAuditLogService
from app.services.admin_auth import AdminPrincipal
from app.services.admin_model_catalog import (
    AdminModelCatalogError,
    AdminModelCatalogSnapshot,
    AdminModelCatalogService,
    AdminModelConfigItem,
    InMemoryAdminModelCatalogStore,
    ModelVisibilityScope,
    SqlAlchemyAdminModelCatalogStore,
)
from app.services.model_catalog_cache import (
    ModelCacheInvalidationEvent,
    ModelCacheMetricEvent,
    RedisModelCatalogCache,
    shared_model_cache_metrics,
    shared_model_catalog_cache,
)
from app.services.runtime_models import SqlAlchemyRuntimeModelStore
from app.services.runtime_workspace import Principal
from app.services.user_directory import UserDirectoryService

router = APIRouter(prefix="/model-config", tags=["admin-model-config"])
_catalog_store = InMemoryAdminModelCatalogStore()


async def get_admin_model_catalog_service(db: AsyncSession = Depends(get_db)) -> AdminModelCatalogService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return AdminModelCatalogService(SqlAlchemyAdminModelCatalogStore(db))
    return AdminModelCatalogService(_catalog_store)


def _catalog_snapshot_response(snapshot: AdminModelCatalogSnapshot) -> AdminModelConfigResponse:
    latest_release = None
    if snapshot.latest_release is not None:
        latest_release = AdminModelCatalogReleaseSummary(
            release_id=snapshot.latest_release.release_id,
            catalog_version=snapshot.latest_release.catalog_version,
            previous_catalog_version=snapshot.latest_release.previous_catalog_version,
            config_hash=snapshot.latest_release.config_hash,
            status=snapshot.latest_release.status,
            error_message=snapshot.latest_release.error_message,
            started_at=snapshot.latest_release.started_at,
            finished_at=snapshot.latest_release.finished_at,
        )
    return AdminModelConfigResponse(
        entries=[
            AdminModelConfigEntrySummary(
                provider_id=entry.provider_id,
                provider_name=entry.provider_key,
                provider_display_name=entry.provider_display_name,
                provider_type=entry.provider_type,
                api_endpoint=entry.api_endpoint,
                credential_configured=entry.api_key_configured,
                provider_status=entry.provider_status,
                provider_catalog_version=entry.provider_catalog_version,
                model_entry_id=entry.model_entry_id,
                model_key=entry.model_key,
                model_display_name=entry.model_display_name,
                context_length=entry.context_length,
                tags=entry.tags,
                is_restricted=(
                    not entry.visibility_scope.organization
                    if entry.visibility_scope is not None
                    else entry.is_restricted
                ),
                visibility_scope=AdminModelVisibilityScope(
                    organization=entry.visibility_scope.organization,
                    user_ids=entry.visibility_scope.user_ids,
                    department_ids=entry.visibility_scope.department_ids,
                ),
                visibility_users=[
                    AdminModelVisibilityUserSummary(
                        id=user.id,
                        name=user.name,
                        user_code=user.user_code,
                        status=user.status,
                    )
                    for user in entry.visibility_users
                ],
                is_default=entry.is_default,
                model_status=entry.model_status,
                model_catalog_version=entry.model_catalog_version,
                runtime_capabilities=entry.runtime_capabilities,
            )
            for entry in snapshot.entries
        ],
        latest_release=latest_release,
    )


@router.get("", response_model=AdminModelConfigResponse)
async def get_model_config(
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminModelCatalogService = Depends(get_admin_model_catalog_service),
) -> AdminModelConfigResponse:
    return _catalog_snapshot_response(await service.list_catalog(principal=principal))


@router.get(
    "/directory/tree",
    response_model=list[OrganizationDirectoryNode],
)
async def get_model_visibility_directory_tree(
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationDirectoryNode]:
    return await UserDirectoryService(db).organization_directory(
        principal=Principal(
            org_id=principal.org_id,
            user_id=principal.user_id,
        ),
        keyword="",
    )


@router.put("", response_model=AdminModelCatalogPublishResponse)
async def publish_model_config(
    payload: AdminModelCatalogPublishRequest,
    db: AsyncSession = Depends(get_db),
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminModelCatalogService = Depends(get_admin_model_catalog_service),
    audit_service: AdminAuditLogService = Depends(get_admin_audit_log_service),
    model_cache: RedisModelCatalogCache = Depends(shared_model_catalog_cache),
) -> AdminModelCatalogPublishResponse:
    try:
        intent = await service.publish_catalog(
            principal=principal,
            entries=[
                AdminModelConfigItem(
                    provider_key=entry.provider_name,
                    provider_display_name=entry.provider_display_name,
                    provider_type=entry.provider_type,
                    api_endpoint=entry.api_endpoint,
                    api_key=entry.provider_credential,
                    model_key=entry.model_key,
                    model_display_name=entry.model_display_name,
                    context_length=entry.context_length,
                    tags=entry.tags,
                    is_restricted=(
                        not entry.visibility_scope.organization
                        if entry.visibility_scope is not None
                        else entry.is_restricted
                    ),
                visibility_scope=(
                    ModelVisibilityScope(
                        organization=entry.visibility_scope.organization,
                        user_ids=entry.visibility_scope.user_ids,
                        department_ids=entry.visibility_scope.department_ids,
                    )
                    if entry.visibility_scope is not None
                    else None
                ),
                model_status=entry.model_status,
                    is_default=entry.is_default,
                    runtime_capabilities=entry.runtime_capabilities.model_dump(),
                )
                for entry in payload.entries
            ],
        )
    except AdminModelCatalogError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        shared_model_cache_metrics().record(
            ModelCacheMetricEvent(
                cache_name="model_publish",
                event="failure",
                error_type=exc.__class__.__name__,
            )
        )
        try:
            await audit_service.record(
                principal=principal,
                action="admin.model.catalog_publish_failed",
                target_type="model_catalog",
                target_id="pending",
                detail={
                    "status": "failed",
                    "entry_count": len(payload.entries),
                    "error_type": exc.__class__.__name__,
                },
            )
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="model catalog publish failed") from exc
    await audit_service.record(
        principal=principal,
        action="admin.model.catalog_published",
        target_type="model_catalog",
        target_id=intent.release_id,
        detail={
            "catalog_version": intent.catalog_version,
            "previous_catalog_version": intent.previous_catalog_version,
            "config_hash": intent.config_hash,
            "status": intent.status,
            "entry_count": len(payload.entries),
            "reasoning_protocol_enabled_model_ids": list(intent.reasoning_protocol_enabled_model_ids),
            "reasoning_protocol_disabled_model_ids": list(intent.reasoning_protocol_disabled_model_ids),
            "reasoning_protocol_enabled_count": len(intent.reasoning_protocol_enabled_model_ids),
            "reasoning_protocol_disabled_count": len(intent.reasoning_protocol_disabled_model_ids),
            "restricted_model_count": intent.restricted_model_count,
            "user_grant_count": intent.user_grant_count,
            "department_grant_count": intent.department_grant_count,
        },
    )
    if settings.RUNTIME_CHAT_MODE == "real":
        try:
            await model_cache.invalidate(
                ModelCacheInvalidationEvent.catalog_published(
                    org_id=principal.org_id,
                    catalog_version=intent.catalog_version,
                    model_entry_ids=intent.model_entry_ids,
                    provider_ids=intent.provider_ids,
                )
            )
            await SqlAlchemyRuntimeModelStore(db, cache=model_cache).prewarm_visible_models(
                org_id=principal.org_id,
                user_id=principal.user_id,
            )
        except RedisError:
            pass
        except Exception:
            # Publication already committed.  Prewarm is a bounded best-effort
            # optimization; normal read paths and TTL/lock controls remain the
            # correctness boundary if prewarm cannot complete.
            pass
    return AdminModelCatalogPublishResponse(
        release_id=intent.release_id,
        catalog_version=intent.catalog_version,
        previous_catalog_version=intent.previous_catalog_version,
        config_hash=intent.config_hash,
        status=intent.status,
        published_at=intent.published_at,
    )
