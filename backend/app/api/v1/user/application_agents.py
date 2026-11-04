from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.api.v1.application_agent_deps import get_application_agent_admin_principal
from app.api.v1.application_agent_role_deps import get_application_agent_authorization_service
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.schemas.application_agent import (
    ApplicationAgentConfig,
    ApplicationAgentAccessSummary,
    ApplicationAgentConfigRevisionSummary,
    ApplicationAgentCreateRequest,
    ApplicationAgentDetail,
    ApplicationAgentPersonalKnowledgeCandidate,
    ApplicationAgentPersonalKnowledgeCatalog,
    ApplicationAgentSummary,
    ApplicationAgentSkillSummary,
    ApplicationAgentSkillPolicyUpdateRequest,
    ApplicationAgentSkillPolicyUpdateResponse,
    ApplicationAgentUpdateRequest,
    ApplicationClientCreated,
    ApplicationClientCreateRequest,
    ApplicationClientSummary,
    ApplicationClientUpdateRequest,
)
from app.schemas.personal_skills import SkillDescriptionUpdateRequest
from app.schemas.application_agent_authorization import (
    ApplicationAgentAuthorizationCodeResponse,
    ApplicationAgentAuthorizationRequest,
)
from app.schemas.application_agent_invocation import (
    ApplicationAgentStudioHistory,
)
from app.schemas.runtime_chat import (
    CreateRuntimeSessionRequest,
    RenameRuntimeSessionRequest,
    RuntimeSessionSummary,
)
from app.schemas.runtime_workspace import RuntimeWorkspaceSummary
from app.schemas.runtime_edge import (
    RuntimeEdgeBootstrapResponse,
    RuntimeEdgeGrantRequest,
    RuntimeEdgeSessionGrantResponse,
)
from app.services.admin_audit import AdminAuditLogService
from app.services.admin_auth import AdminPrincipal
from app.services.application_agents import (
    ApplicationAgentError,
    ApplicationAgentRecord,
    ApplicationAgentRevisionRecord,
    ApplicationClientRecord,
    InMemoryApplicationAgentService,
    SqlAlchemyApplicationAgentService,
)
from app.services.application_agent_admin import ApplicationAgentAuthorizationService
from app.services.application_agent_authorization import (
    ApplicationAgentAuthorizationError,
    InMemoryApplicationAgentAuthorizationService,
    SqlAlchemyApplicationAgentAuthorizationService,
)
from app.services.application_agent_runtime import (
    ApplicationAgentRuntimeError,
    NoopApplicationAgentRuntimeManager,
    SqlAlchemyApplicationAgentRuntimeManager,
)
from app.services.application_agent_skills import (
    ApplicationAgentSkillError,
    ApplicationAgentSkillRecord,
    InMemoryApplicationAgentSkillService,
    MAX_APPLICATION_SKILL_PACKAGE_BYTES,
    SqlAlchemyApplicationAgentSkillService,
)
from app.services.application_agent_invocation import (
    ApplicationAgentInvocationError,
    InMemoryApplicationAgentInvocationService,
    SqlAlchemyApplicationAgentInvocationService,
)
from app.services.application_agent_personal_knowledge import (
    ApplicationAgentPersonalKnowledgeService,
    ApplicationPersonalKnowledgeCatalog,
)
from app.services.runtime_workspace import Principal
from app.services.runtime_edge_grants import (
    RuntimeEdgeGrantError,
    RuntimeEdgeGrantService,
)
from app.api.v1.user.runtime_edge import _trusted_origin
from app.services.runtime_prepared_knowledge import (
    PreparedKnowledgeStoreUnavailable,
    shared_runtime_prepared_knowledge_service,
)


router = APIRouter(prefix="/application-agents", tags=["user-application-agents"])
application_agent_memory_service = InMemoryApplicationAgentService()
application_agent_noop_runtime_manager = NoopApplicationAgentRuntimeManager()
application_agent_skill_memory_service = (
    InMemoryApplicationAgentSkillService()
)
application_agent_authorization_memory_service = (
    InMemoryApplicationAgentAuthorizationService(
        application_agent_memory_service
    )
)
application_agent_studio_invocation_memory_service = (
    InMemoryApplicationAgentInvocationService(
        application_agent_memory_service
    )
)


async def get_application_agent_service(db: AsyncSession = Depends(get_db)):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentService(db)
    return application_agent_memory_service


async def get_application_agent_runtime_manager(
    db: AsyncSession = Depends(get_db),
):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentRuntimeManager(db)
    return application_agent_noop_runtime_manager


async def get_application_agent_skill_service(
    db: AsyncSession = Depends(get_db),
):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentSkillService(db)
    return application_agent_skill_memory_service


async def get_application_agent_personal_knowledge_service(
    db: AsyncSession = Depends(get_db),
) -> ApplicationAgentPersonalKnowledgeService:
    return ApplicationAgentPersonalKnowledgeService(db)


async def get_application_agent_authorization_code_service(
    db: AsyncSession = Depends(get_db),
):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentAuthorizationService(db)
    return application_agent_authorization_memory_service


async def get_application_agent_studio_invocation_service(
    db: AsyncSession = Depends(get_db),
):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentInvocationService(
            db,
            prepared_knowledge_service=(
                shared_runtime_prepared_knowledge_service()
            ),
        )
    return application_agent_studio_invocation_memory_service


def _summary(record: ApplicationAgentRecord) -> ApplicationAgentSummary:
    return ApplicationAgentSummary(
        application_agent_id=record.application_agent_id,
        slug=record.slug,
        name=record.name,
        avatar_url=record.avatar_url,
        status=record.status,
        runtime_status=record.runtime_status,
        config_generation=record.config_generation,
        applied_generation=record.applied_generation,
        apply_status=record.apply_status,
        lifecycle_status=record.lifecycle_status,
        delete_requested_at=record.delete_requested_at,
        delete_last_error_code=record.delete_last_error_code,
        client_count=record.client_count,
        active_run_count=record.active_run_count,
        queued_run_count=record.queued_run_count,
        published_at=record.published_at,
        updated_at=record.updated_at,
        access_mode=record.access_mode,
        creator_name=record.creator_name,
        creator_user_code=record.creator_user_code,
        creator_status=record.creator_status,
        personal_knowledge_enabled_count=(
            record.personal_knowledge_enabled_count
        ),
        personal_knowledge_revoked_count=(
            record.personal_knowledge_revoked_count
        ),
    )


def _detail(record: ApplicationAgentRecord) -> ApplicationAgentDetail:
    return ApplicationAgentDetail(
        **_summary(record).model_dump(),
        config=ApplicationAgentConfig.model_validate(record.config),
        last_apply_error_code=record.last_apply_error_code,
    )


def _personal_knowledge_catalog(
    record: ApplicationPersonalKnowledgeCatalog,
) -> ApplicationAgentPersonalKnowledgeCatalog:
    return ApplicationAgentPersonalKnowledgeCatalog(
        items=[
            ApplicationAgentPersonalKnowledgeCandidate(
                knowledge_base_id=item.knowledge_base_id,
                name=item.name,
                description=item.description,
                owner_user_id=item.owner_user_id,
                owner_name=item.owner_name,
                effective_role=item.effective_role,
                file_count=item.file_count,
                ready_file_count=item.ready_file_count,
                visibility=item.visibility,
                status=item.status,
                created_at=item.created_at,
                updated_at=item.updated_at,
                selected=item.selected,
            )
            for item in record.items
        ],
        enabled_count=record.enabled_count,
        revoked_count=record.revoked_count,
        config_generation=record.config_generation,
    )


def _is_market_skill_only_update(
    before: ApplicationAgentRecord,
    after: ApplicationAgentRecord,
) -> bool:
    if (
        before.name != after.name
        or before.avatar_url != after.avatar_url
    ):
        return False
    before_config = dict(before.config)
    after_config = dict(after.config)
    before_skill_ids = list(
        before_config.pop("skill_asset_ids", []) or []
    )
    after_skill_ids = list(
        after_config.pop("skill_asset_ids", []) or []
    )
    return (
        before_skill_ids != after_skill_ids
        and before_config == after_config
    )


def _is_settings_only_update(
    before: ApplicationAgentRecord,
    after: ApplicationAgentRecord,
) -> bool:
    if (
        before.name != after.name
        or before.avatar_url != after.avatar_url
    ):
        return False
    before_config = dict(before.config)
    after_config = dict(after.config)
    settings_keys = {
        "system_prompt",
        "model_entry_id",
        "memory_enabled",
    }
    changed_keys = {
        key
        for key in before_config.keys() | after_config.keys()
        if before_config.get(key) != after_config.get(key)
    }
    return bool(changed_keys) and changed_keys <= settings_keys


def _revision(record: ApplicationAgentRevisionRecord) -> ApplicationAgentConfigRevisionSummary:
    return ApplicationAgentConfigRevisionSummary(
        revision_id=record.revision_id,
        generation=record.generation,
        config_hash=record.config_hash,
        apply_status=record.apply_status,
        applied_at=record.applied_at,
        error_code=record.error_code,
        created_by_user_id=record.created_by_user_id,
        created_at=record.created_at,
    )


def _skill(
    record: ApplicationAgentSkillRecord,
) -> ApplicationAgentSkillSummary:
    return ApplicationAgentSkillSummary(
        application_agent_skill_id=record.application_agent_skill_id,
        application_agent_id=record.application_agent_id,
        slug=record.slug,
        name=record.name,
        description=record.description,
        category_key=record.category_key,
        package_sha256=record.package_sha256,
        skill_version_id=record.skill_version_id,
        version=record.version,
        created_at=record.created_at,
    )


def _client(record: ApplicationClientRecord) -> ApplicationClientSummary:
    return ApplicationClientSummary(
        application_client_id=record.application_client_id,
        client_id=record.client_id,
        name=record.name,
        status=record.status,
        allow_user_delegation=record.allow_user_delegation,
        allow_service_invocation=record.allow_service_invocation,
        auth_mode=record.auth_mode,
        scopes=record.scopes,
        allowed_redirect_uris=record.allowed_redirect_uris,
        created_at=record.created_at,
    )


def _created_client(record: ApplicationClientRecord) -> ApplicationClientCreated:
    if record.client_secret is None:
        raise RuntimeError("new application client secret is unavailable")
    return ApplicationClientCreated(
        **_client(record).model_dump(),
        client_secret=record.client_secret,
    )


def _http_error(exc: ApplicationAgentError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


def _audit_principal(principal: Principal) -> AdminPrincipal:
    return AdminPrincipal(
        org_id=principal.org_id,
        user_id=principal.user_id,
        roles=("application_agent_admin",),
    )


def _studio_workspace(
    record: ApplicationAgentRecord,
    principal: Principal,
) -> RuntimeWorkspaceSummary:
    return RuntimeWorkspaceSummary(
        runtime_workspace_id=record.application_agent_id,
        cloud_profile_id=record.application_agent_id,
        org_id=record.org_id,
        user_id=principal.user_id,
        name=record.name,
        status="active",
        default_model_id=record.config.get("model_entry_id"),
        runtime_status=(
            "ready"
            if record.runtime_status == "absent"
            and settings.RUNTIME_CHAT_MODE != "real"
            else record.runtime_status
        ),
        pending_reload=record.apply_status == "applying",
        updated_at=record.updated_at,
    )


def _studio_session(
    record: dict,
    *,
    agent_id: int,
) -> RuntimeSessionSummary:
    return RuntimeSessionSummary(
        session_id=0,
        runtime_workspace_id=agent_id,
        runtime_session_id=str(record["runtime_session_id"]),
        title=str(record.get("title") or "新任务"),
        source_type=str(record.get("source_type") or "web"),
        message_count=int(record.get("message_count") or 0),
        status="active",
        last_active_at=record.get("last_active_at"),
        agent_display_name="应用 Agent",
        agent_profile_kind="application",
        is_read_only=bool(record.get("is_read_only")),
        is_immutable=bool(record.get("is_immutable")),
        origin_label=(
            str(record["origin_label"])
            if record.get("origin_label")
            else None
        ),
        latest_run=record.get("latest_run"),
    )


@router.get("", response_model=list[ApplicationAgentSummary])
async def list_application_agents(
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
) -> list[ApplicationAgentSummary]:
    return [_summary(record) for record in await service.list_agents(principal=principal)]


@router.post("", response_model=ApplicationAgentDetail, status_code=201)
async def create_application_agent(
    payload: ApplicationAgentCreateRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentDetail:
    try:
        record = await service.create_agent(principal=principal, payload=payload)
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.created",
        target_type="application_agent",
        target_id=record.application_agent_id,
        detail={"slug": record.slug},
    )
    return _detail(record)


@router.get("/access", response_model=ApplicationAgentAccessSummary)
async def get_application_agent_access(
    principal: Principal = Depends(get_principal),
    authorization: ApplicationAgentAuthorizationService = Depends(
        get_application_agent_authorization_service
    ),
) -> ApplicationAgentAccessSummary:
    return ApplicationAgentAccessSummary(
        can_manage_application_agents=await authorization.can_manage(principal)
    )


@router.post(
    "/authorizations",
    response_model=ApplicationAgentAuthorizationCodeResponse,
)
async def create_application_agent_authorization(
    payload: ApplicationAgentAuthorizationRequest,
    principal: Principal = Depends(get_principal),
    service=Depends(get_application_agent_authorization_code_service),
) -> ApplicationAgentAuthorizationCodeResponse:
    """Create a short-lived, one-time code after the user's Enterprise SSO login."""
    try:
        code, expires_in = await service.authorize(
            principal=principal,
            agent_slug=payload.application_agent,
            client_id=payload.client_id,
            redirect_uri=payload.redirect_uri,
        )
    except ApplicationAgentAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=(
                {"code": exc.code, "redirect_uri": exc.redirect_uri}
                if exc.redirect_uri is not None
                else exc.code
            ),
        ) from exc
    return ApplicationAgentAuthorizationCodeResponse(
        code=code,
        expires_in=expires_in,
        redirect_uri=payload.redirect_uri,
    )


@router.get("/{agent_id}", response_model=ApplicationAgentDetail)
async def get_application_agent(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
) -> ApplicationAgentDetail:
    try:
        return _detail(await service.get_agent(principal=principal, agent_id=agent_id))
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{agent_id}/personal-knowledge",
    response_model=ApplicationAgentPersonalKnowledgeCatalog,
)
async def list_application_agent_personal_knowledge(
    agent_id: int,
    response: Response,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    service: ApplicationAgentPersonalKnowledgeService = Depends(
        get_application_agent_personal_knowledge_service
    ),
) -> ApplicationAgentPersonalKnowledgeCatalog:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return _personal_knowledge_catalog(
            await service.catalog(
                principal=principal,
                agent_id=agent_id,
            )
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc


@router.put(
    "/{agent_id}/personal-knowledge/{knowledge_base_id}",
    response_model=ApplicationAgentPersonalKnowledgeCatalog,
)
async def select_application_agent_personal_knowledge(
    agent_id: int,
    knowledge_base_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    agent_service=Depends(get_application_agent_service),
    service: ApplicationAgentPersonalKnowledgeService = Depends(
        get_application_agent_personal_knowledge_service
    ),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentPersonalKnowledgeCatalog:
    try:
        agent = await agent_service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        catalog = await service.select(
            principal=principal,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
        )
        if agent.runtime_status != "absent":
            await runtime_manager.ensure_runtime(
                org_id=principal.org_id,
                agent_id=agent_id,
                apply_scope="settings_only",
            )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.personal_knowledge_selected",
        target_type="application_agent",
        target_id=agent_id,
        detail={"knowledge_base_id": knowledge_base_id},
    )
    return _personal_knowledge_catalog(catalog)


@router.delete(
    "/{agent_id}/personal-knowledge/{knowledge_base_id}",
    response_model=ApplicationAgentPersonalKnowledgeCatalog,
)
async def disable_application_agent_personal_knowledge(
    agent_id: int,
    knowledge_base_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    agent_service=Depends(get_application_agent_service),
    service: ApplicationAgentPersonalKnowledgeService = Depends(
        get_application_agent_personal_knowledge_service
    ),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentPersonalKnowledgeCatalog:
    try:
        agent = await agent_service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        catalog = await service.disable(
            principal=principal,
            agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
        )
        if agent.runtime_status != "absent":
            await runtime_manager.ensure_runtime(
                org_id=principal.org_id,
                agent_id=agent_id,
                apply_scope="settings_only",
            )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.personal_knowledge_disabled",
        target_type="application_agent",
        target_id=agent_id,
        detail={"knowledge_base_id": knowledge_base_id},
    )
    return _personal_knowledge_catalog(catalog)


@router.get(
    "/{agent_id}/skills",
    response_model=list[ApplicationAgentSkillSummary],
)
async def list_application_agent_skills(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    skill_service=Depends(get_application_agent_skill_service),
) -> list[ApplicationAgentSkillSummary]:
    try:
        await service.get_agent(principal=principal, agent_id=agent_id)
        records = await skill_service.list_skills(
            principal=principal,
            agent_id=agent_id,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentSkillError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return [_skill(record) for record in records]


@router.post(
    "/{agent_id}/skills/upload",
    response_model=ApplicationAgentSkillSummary,
    status_code=201,
)
async def upload_application_agent_skill(
    agent_id: int,
    file: UploadFile = File(...),
    category_key: str = Form("personal"),
    description: str | None = Form(None),
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    skill_service=Depends(get_application_agent_skill_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
) -> ApplicationAgentSkillSummary:
    uploaded: ApplicationAgentSkillRecord | None = None
    try:
        agent = await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        content = await file.read(
            MAX_APPLICATION_SKILL_PACKAGE_BYTES + 1
        )
        uploaded = await skill_service.upload_skill(
            principal=principal,
            agent_id=agent_id,
            content=content,
            category_key=category_key,
            description=description if isinstance(description, str) else None,
        )
        config = ApplicationAgentConfig.model_validate(agent.config)
        application_skill_ids = list(config.application_skill_ids)
        if uploaded.application_agent_skill_id not in application_skill_ids:
            application_skill_ids.append(
                uploaded.application_agent_skill_id
            )
        updated = await service.update_agent(
            principal=principal,
            agent_id=agent_id,
            payload=ApplicationAgentUpdateRequest(
                name=agent.name,
                avatar_url=agent.avatar_url,
                config=config.model_copy(
                    update={
                        "application_skill_ids": application_skill_ids
                    }
                ),
                expected_config_generation=agent.config_generation,
            ),
        )
        if updated.runtime_status != "absent":
            await runtime_manager.ensure_runtime(
                org_id=principal.org_id,
                agent_id=agent_id,
                apply_scope="skills_only",
            )
    except ApplicationAgentSkillError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    except ApplicationAgentError as exc:
        if uploaded is not None:
            await skill_service.delete_skill(
                principal=principal,
                agent_id=agent_id,
                skill_id=uploaded.application_agent_skill_id,
            )
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        # The desired config and upload source remain intact. The normal
        # ensure_runtime path can retry without asking the user to upload the
        # package again.
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    assert uploaded is not None
    return _skill(uploaded)


@router.put(
    "/{agent_id}/skills/{skill_id}",
    response_model=ApplicationAgentSkillSummary,
)
async def update_application_agent_skill_description(
    agent_id: int,
    skill_id: int,
    payload: SkillDescriptionUpdateRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    skill_service=Depends(get_application_agent_skill_service),
) -> ApplicationAgentSkillSummary:
    try:
        await service.get_owned_agent(principal=principal, agent_id=agent_id)
        record = await skill_service.update_description(
            principal=principal,
            agent_id=agent_id,
            skill_id=skill_id,
            description=payload.description,
            name=payload.name,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentSkillError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return _skill(record)


@router.delete(
    "/{agent_id}/skills/{skill_id}",
    status_code=204,
    response_class=Response,
)
async def delete_application_agent_skill(
    agent_id: int,
    skill_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    skill_service=Depends(get_application_agent_skill_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
) -> Response:
    try:
        agent = await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        await skill_service.get_skill(
            principal=principal,
            agent_id=agent_id,
            skill_id=skill_id,
        )
        config = ApplicationAgentConfig.model_validate(agent.config)
        if skill_id in config.application_skill_ids:
            agent = await service.update_agent(
                principal=principal,
                agent_id=agent_id,
                payload=ApplicationAgentUpdateRequest(
                    name=agent.name,
                    avatar_url=agent.avatar_url,
                    config=config.model_copy(
                        update={
                            "application_skill_ids": [
                                item
                                for item in config.application_skill_ids
                                if item != skill_id
                            ]
                        }
                    ),
                    expected_config_generation=agent.config_generation,
                ),
            )
        if agent.runtime_status != "absent":
            await runtime_manager.ensure_runtime(
                org_id=principal.org_id,
                agent_id=agent_id,
                apply_scope="skills_only",
            )
        await skill_service.delete_skill(
            principal=principal,
            agent_id=agent_id,
            skill_id=skill_id,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentSkillError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    except ApplicationAgentRuntimeError as exc:
        # Keep the source row and files until Runtime has converged. Retrying
        # the same DELETE safely completes cleanup.
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return Response(status_code=204)


@router.delete(
    "/{agent_id}",
    status_code=202,
    response_class=Response,
)
async def delete_application_agent(
    agent_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    service=Depends(get_application_agent_service),
) -> Response:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        await service.prepare_delete(
            principal=principal,
            agent_id=agent_id,
        )
        from app.im_gateway.coordinator import im_gateway

        im_gateway.request_reconcile()
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=202)


@router.put("/{agent_id}", response_model=ApplicationAgentDetail)
async def update_application_agent(
    agent_id: int,
    payload: ApplicationAgentUpdateRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentDetail:
    try:
        previous = await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        record = await service.update_agent(principal=principal, agent_id=agent_id, payload=payload)
        if record.runtime_status != "absent":
            apply_scope = (
                "skills_only"
                if _is_market_skill_only_update(previous, record)
                else "settings_only"
                if _is_settings_only_update(previous, record)
                else "full"
            )
            await runtime_manager.ensure_runtime(
                org_id=principal.org_id,
                agent_id=agent_id,
                apply_scope=apply_scope,
            )
            record = await service.get_agent(
                principal=principal,
                agent_id=agent_id,
            )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.config_saved",
        target_type="application_agent",
        target_id=agent_id,
        detail={"generation": record.config_generation},
    )
    return _detail(record)


@router.put(
    "/{agent_id}/skill-policy",
    response_model=ApplicationAgentSkillPolicyUpdateResponse,
)
async def update_application_agent_skill_policy(
    agent_id: int,
    payload: ApplicationAgentSkillPolicyUpdateRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentSkillPolicyUpdateResponse:
    try:
        record = await service.update_skill_policy(
            principal=principal,
            agent_id=agent_id,
            policy_version=payload.policy_version,
            expected_config_generation=payload.expected_config_generation,
        )
        if record.runtime_status != "absent":
            await runtime_manager.ensure_runtime(
                org_id=principal.org_id,
                agent_id=agent_id,
                apply_scope="skills_only",
            )
            record = await service.get_agent(principal=principal, agent_id=agent_id)
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.skill_policy_updated",
        target_type="application_agent",
        target_id=agent_id,
        detail={"policy_version": payload.policy_version, "generation": record.config_generation},
    )
    return ApplicationAgentSkillPolicyUpdateResponse(
        application_agent_id=record.application_agent_id,
        policy_version=payload.policy_version,
        config_generation=record.config_generation,
        applied_generation=record.applied_generation,
        apply_status=record.apply_status,
        pending_reload=record.apply_status == "applying",
    )


@router.get(
    "/{agent_id}/studio/workspace",
    response_model=RuntimeWorkspaceSummary,
)
async def get_application_agent_studio_workspace(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
) -> RuntimeWorkspaceSummary:
    try:
        record = await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    return _studio_workspace(record, principal)


@router.post(
    "/{agent_id}/studio/warmup",
    response_model=RuntimeWorkspaceSummary,
)
@router.post(
    "/{agent_id}/studio/keepalive",
    response_model=RuntimeWorkspaceSummary,
)
async def warm_application_agent_studio_workspace(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
) -> RuntimeWorkspaceSummary:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        await runtime_manager.ensure_runtime_available(
            org_id=principal.org_id,
            agent_id=agent_id,
        )
        record = await service.get_agent(
            principal=principal,
            agent_id=agent_id,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return _studio_workspace(record, principal)


@router.get(
    "/{agent_id}/studio/sessions",
    response_model=list[RuntimeSessionSummary],
)
async def list_application_agent_studio_sessions(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    invocation=Depends(get_application_agent_studio_invocation_service),
) -> list[RuntimeSessionSummary]:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        await runtime_manager.ensure_runtime_available(
            org_id=principal.org_id,
            agent_id=agent_id,
        )
        records = await invocation.list_studio_sessions(
            principal=principal,
            agent_id=agent_id,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except (
        ApplicationAgentRuntimeError,
        ApplicationAgentInvocationError,
    ) as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return [_studio_session(item, agent_id=agent_id) for item in records]


@router.post(
    "/{agent_id}/studio/sessions",
    response_model=RuntimeSessionSummary,
    status_code=201,
)
async def create_application_agent_studio_session(
    agent_id: int,
    payload: CreateRuntimeSessionRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    invocation=Depends(get_application_agent_studio_invocation_service),
) -> RuntimeSessionSummary:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        await runtime_manager.ensure_runtime_available(
            org_id=principal.org_id,
            agent_id=agent_id,
        )
        record = await invocation.create_studio_session(
            principal=principal,
            agent_id=agent_id,
            title=payload.title,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except (
        ApplicationAgentRuntimeError,
        ApplicationAgentInvocationError,
    ) as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return _studio_session(record, agent_id=agent_id)


@router.patch(
    "/{agent_id}/studio/sessions/{runtime_session_id}",
    response_model=RuntimeSessionSummary,
)
async def rename_application_agent_studio_session(
    agent_id: int,
    runtime_session_id: str,
    payload: RenameRuntimeSessionRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    invocation=Depends(get_application_agent_studio_invocation_service),
) -> RuntimeSessionSummary:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        record = await invocation.rename_studio_session(
            principal=principal,
            agent_id=agent_id,
            runtime_session_id=runtime_session_id,
            title=payload.title,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentInvocationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return _studio_session(record, agent_id=agent_id)


@router.delete(
    "/{agent_id}/studio/sessions/{runtime_session_id}",
    status_code=204,
    response_class=Response,
)
async def delete_application_agent_studio_session(
    agent_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    invocation=Depends(get_application_agent_studio_invocation_service),
) -> Response:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        await invocation.delete_studio_session(
            principal=principal,
            agent_id=agent_id,
            runtime_session_id=runtime_session_id,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentInvocationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return Response(status_code=204)


@router.post(
    "/{agent_id}/studio/runtime-edge/prepare",
    response_model=RuntimeEdgeBootstrapResponse,
)
async def prepare_application_agent_runtime_edge(
    agent_id: int,
    payload: RuntimeEdgeGrantRequest,
    request: Request,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    db: AsyncSession = Depends(get_db),
) -> RuntimeEdgeBootstrapResponse:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        await runtime_manager.ensure_runtime_available(
            org_id=principal.org_id,
            agent_id=agent_id,
        )
        grant_service = RuntimeEdgeGrantService(db)
        target = await grant_service.application_target(
            principal=principal,
            application_agent_id=agent_id,
            request=payload,
        )
        await shared_runtime_prepared_knowledge_service().prepare_application_agent_studio(
            application_agent_id=agent_id,
            subject_user_id=principal.user_id,
            runtime_session_id=target.runtime_session_id,
            runtime_token=target.api_server_key,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    except RuntimeEdgeGrantError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except PreparedKnowledgeStoreUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="application_studio_knowledge_unavailable",
        ) from exc
    return grant_service.issue_bootstrap(
        principal=principal,
        target=target,
        allowed_origin=_trusted_origin(request),
    )


@router.post(
    "/{agent_id}/studio/runtime-edge/refresh-grant",
    response_model=RuntimeEdgeSessionGrantResponse,
)
async def refresh_application_agent_runtime_edge_grant(
    agent_id: int,
    payload: RuntimeEdgeGrantRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    db: AsyncSession = Depends(get_db),
) -> RuntimeEdgeSessionGrantResponse:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        grant_service = RuntimeEdgeGrantService(db)
        target = await grant_service.application_target(
            principal=principal,
            application_agent_id=agent_id,
            request=payload,
        )
        await shared_runtime_prepared_knowledge_service().prepare_application_agent_studio(
            application_agent_id=agent_id,
            subject_user_id=principal.user_id,
            runtime_session_id=target.runtime_session_id,
            runtime_token=target.api_server_key,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except RuntimeEdgeGrantError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except PreparedKnowledgeStoreUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="application_studio_knowledge_unavailable",
        ) from exc
    return grant_service.issue_session_grant(
        principal=principal,
        target=target,
    )


@router.get(
    "/{agent_id}/studio/sessions/{runtime_session_id}/history",
    response_model=ApplicationAgentStudioHistory,
)
async def get_application_agent_studio_history(
    agent_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    invocation=Depends(get_application_agent_studio_invocation_service),
) -> ApplicationAgentStudioHistory:
    try:
        await service.get_owned_agent(
            principal=principal,
            agent_id=agent_id,
        )
        messages = await invocation.studio_history(
            principal=principal,
            agent_id=agent_id,
            runtime_session_id=runtime_session_id,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentInvocationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return ApplicationAgentStudioHistory(
        runtime_session_id=runtime_session_id,
        messages=messages,
    )


@router.get("/{agent_id}/revisions", response_model=list[ApplicationAgentConfigRevisionSummary])
async def list_application_agent_revisions(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
) -> list[ApplicationAgentConfigRevisionSummary]:
    try:
        return [
            _revision(record)
            for record in await service.list_revisions(principal=principal, agent_id=agent_id)
        ]
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc


@router.get("/{agent_id}/clients", response_model=list[ApplicationClientSummary])
async def list_application_agent_clients(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
) -> list[ApplicationClientSummary]:
    try:
        return [
            _client(record)
            for record in await service.list_clients(principal=principal, agent_id=agent_id)
        ]
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc


@router.post("/{agent_id}/clients", response_model=ApplicationClientCreated, status_code=201)
async def create_application_agent_client(
    agent_id: int,
    payload: ApplicationClientCreateRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationClientCreated:
    try:
        record = await service.create_client(principal=principal, agent_id=agent_id, payload=payload)
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.client_created",
        target_type="application_agent",
        target_id=agent_id,
        detail={"application_client_id": record.application_client_id, "client_id": record.client_id},
    )
    return _created_client(record)


@router.put(
    "/{agent_id}/clients/{client_pk}",
    response_model=ApplicationClientSummary,
)
async def update_application_agent_client(
    agent_id: int,
    client_pk: int,
    payload: ApplicationClientUpdateRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationClientSummary:
    try:
        record = await service.update_client(
            principal=principal,
            agent_id=agent_id,
            client_pk=client_pk,
            payload=payload,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.client_updated",
        target_type="application_agent",
        target_id=agent_id,
        detail={"application_client_id": record.application_client_id},
    )
    return _client(record)


@router.delete(
    "/{agent_id}/clients/{client_pk}",
    status_code=204,
    response_class=Response,
)
async def delete_application_agent_client(
    agent_id: int,
    client_pk: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> Response:
    try:
        await service.delete_client(
            principal=principal,
            agent_id=agent_id,
            client_pk=client_pk,
        )
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.client_deleted",
        target_type="application_agent",
        target_id=agent_id,
        detail={"application_client_id": client_pk},
    )
    return Response(status_code=204)


@router.post("/{agent_id}/publish", response_model=ApplicationAgentDetail)
async def publish_application_agent(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentDetail:
    try:
        if not await service.list_clients(
            principal=principal,
            agent_id=agent_id,
        ):
            raise ApplicationAgentError(
                "application_agent_client_required"
            )
        record = await service.publish(principal=principal, agent_id=agent_id)
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.published",
        target_type="application_agent",
        target_id=agent_id,
        detail={"generation": record.applied_generation},
    )
    return _detail(record)


@router.post("/{agent_id}/disable", response_model=ApplicationAgentDetail)
async def disable_application_agent(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentDetail:
    try:
        record = await service.disable(principal=principal, agent_id=agent_id)
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    from app.im_gateway.coordinator import im_gateway

    im_gateway.request_reconcile()
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.disabled",
        target_type="application_agent",
        target_id=agent_id,
    )
    return _detail(record)


@router.post("/{agent_id}/resume", response_model=ApplicationAgentDetail)
async def resume_application_agent(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_service),
    runtime_manager=Depends(get_application_agent_runtime_manager),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> ApplicationAgentDetail:
    try:
        await runtime_manager.ensure_runtime(
            org_id=principal.org_id,
            agent_id=agent_id,
        )
        record = await service.resume(principal=principal, agent_id=agent_id)
    except ApplicationAgentError as exc:
        raise _http_error(exc) from exc
    except ApplicationAgentRuntimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    from app.im_gateway.coordinator import im_gateway

    im_gateway.request_reconcile()
    await audit.record(
        principal=_audit_principal(principal),
        action="application_agent.resumed",
        target_type="application_agent",
        target_id=agent_id,
    )
    return _detail(record)
