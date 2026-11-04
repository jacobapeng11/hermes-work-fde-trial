from __future__ import annotations

# Proxy variables must be cleared before importing HTTP clients and routers.
# ruff: noqa: E402

import asyncio
import logging
import time
from uuid import uuid4
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator

from app.network_environment import clear_proxy_environment


clear_proxy_environment()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.openapi.application_agents import router as openapi_application_agents_router
from app.api.desktop_updates import router as desktop_updates_router
from app.api.app_center_content import router as app_center_content_router
from app.api.channels.wecom import router as wecom_channel_router
from app.api.v1.runtime_knowledge import router as runtime_knowledge_router
from app.api.v1.application_agent_runtime_knowledge import (
    router as application_agent_runtime_knowledge_router,
)
from app.api.v1.application_agent_runtime_personal_knowledge import (
    router as application_agent_runtime_personal_knowledge_router,
)
from app.api.v1.runtime_personal_knowledge import router as runtime_personal_knowledge_router
from app.api.v1.runtime_kanban import router as runtime_kanban_router
from app.api.v1.runtime_clarify import (
    runtime_router as runtime_clarify_router,
    user_router as user_runtime_clarify_router,
)
from app.api.v1.runtime_experts import router as runtime_experts_router
from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.hermes_runtime.protocol import close_runtime_http_client
from app.api.v1.auth import router as auth_router
from app.api.v1.oauth2_login import router as oauth2_router
from app.services.oauth_access_log import install_oauth_access_log_filter
from app.api.v1.admin.audit_logs import router as admin_audit_logs_router
from app.api.v1.admin.asset_rollouts import router as admin_asset_rollouts_router
from app.api.v1.admin.assets import router as admin_assets_router
from app.api.v1.admin.runtime_admission_audits import router as admin_runtime_admission_audits_router
from app.api.v1.admin.application_agent_admins import router as admin_application_agent_admins_router
from app.api.v1.admin.experts import router as admin_experts_router
from app.api.v1.admin.me import router as admin_me_router
from app.api.v1.admin.models import router as admin_models_router
from app.api.v1.admin.model_config import router as admin_model_config_router
from app.api.v1.admin.kubernetes_resources import router as admin_kubernetes_resources_router
from app.api.v1.admin.knowledge_governance import router as admin_knowledge_governance_router
from app.api.v1.admin.knowledge_providers import router as admin_knowledge_providers_router
from app.api.v1.admin.resources import router as admin_resources_router
from app.api.v1.admin.usage import router as admin_usage_router
from app.api.v1.admin.task_prompt_config import router as admin_task_prompt_config_router
from app.api.v1.user.chat import router as user_chat_router
from app.api.v1.user.apps import router as user_apps_router
from app.api.v1.user.data_apps import (
    launch_router as user_data_app_launch_router,
)
from app.api.v1.user.data_apps import router as user_data_apps_router
from app.api.v1.user.browser_presence import router as user_browser_presence_router
from app.api.v1.user.avatar_assets import router as user_avatar_assets_router
from app.api.v1.user.application_agents import router as user_application_agents_router
from app.api.v1.user.application_agent_im_channels import (
    router as user_application_agent_im_channels_router,
)
from app.api.v1.user.application_agent_wecom import (
    router as user_application_agent_wecom_router,
)
from app.api.v1.user.application_agent_chat_files import (
    router as user_application_agent_chat_files_router,
)
from app.api.v1.user.chat_files import router as user_chat_files_router
from app.api.v1.user.enterprise_knowledge import router as user_enterprise_knowledge_router
from app.api.v1.user.experts import router as user_experts_router
from app.api.v1.user.default_profile_assets import router as default_profile_assets_router
from app.api.v1.user.im_channels import router as user_im_channels_router
from app.api.v1.user.personal_knowledge import router as user_personal_knowledge_router
from app.api.v1.user.personal_skills import router as user_personal_skills_router
from app.api.v1.user.runtime_capabilities import router as runtime_capabilities_router
from app.api.v1.user.runtime_models import router as runtime_models_router
from app.api.v1.user.runtime_sessions import router as runtime_sessions_router
from app.api.v1.user.scheduled_tasks import router as scheduled_tasks_router
from app.api.v1.user.runtime_workspace import router as runtime_workspace_router
from app.api.v1.user.user_directory import router as user_directory_router
from app.api.v1.user.task_prompt_config import router as user_task_prompt_config_router
from app.api.v1.user.wecom_bind import router as user_wecom_bind_router
from app.api.internal.runtime_config_bundles import (
    router as runtime_config_bundles_router,
)
from app.api.internal.profile_artifacts import (
    router as profile_artifacts_router,
)
from app.api.internal.runtime_projections import router as runtime_projections_router
from app.api.internal.runtime_admission_audits import router as runtime_admission_audits_router
from app.api.internal.runtime_edge_auth import router as runtime_edge_auth_router
from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.im_gateway import im_gateway
from app.services.chat_log import ChatLogProjector, SqlAlchemyChatLogStore
from app.services.expert_profile_operations import fail_interrupted_expert_profile_operations
from app.services.expert_release_cleanup import (
    cleanup_expired_expert_release_artifacts,
)
from app.services.expert_collaboration_reconciler import (
    ExpertCollaborationReconciler,
)
from app.services.expert_collaboration_runner import expert_collaboration_runner
from app.services.runtime_projection import (
    delete_expired_runtime_projection_events,
)
from app.services.runtime_clarify import RuntimeClarifyScanner
from app.services.runtime_warm_keepalive import keep_runtimes_warm_forever
from app.services.runtime_infrastructure import build_runtime_protocol_adapters
from app.services.runtime_kanban_leases import RuntimeKanbanLeaseReconciler
from app.services.platform_readiness import (
    DATABASE_TIMEOUT_SECONDS,
    get_platform_readiness,
)
from app.services.platform_session import close_platform_session_store
from app.services.profile_convergence import ProfileConvergenceWorker
from app.services.profile_execution_control import ProfileExecutionControlWorker
from app.services.asset_rollout import AssetRolloutWorker
from app.services.application_agent_asset_rollout import (
    ApplicationAgentAssetRolloutWorker,
)
from app.services.runtime_mcp_registry_convergence import (
    RuntimeMcpRegistryConvergenceWorker,
)
from app.services.runtime_access_nonce import close_runtime_access_nonce_store
from app.services.scheduled_task_scanner import DueTaskScanner
from app.services.application_agent_wecom_callback import (
    process_wecom_callback_event,
    queued_wecom_callback_event_ids,
)
from app.api.v1.user.scheduled_tasks import scheduled_task_launcher
from app.api.v1.user.runtime_edge import router as runtime_edge_router


logger = logging.getLogger(__name__)
startup_logger = logging.getLogger("uvicorn.error")


async def _prewarm_database_pool() -> None:
    started = time.monotonic()
    try:
        target = max(1, int(engine.pool.size()))
    except Exception:
        target = 1
    release = asyncio.Event()
    outcomes: asyncio.Queue[bool] = asyncio.Queue()

    async def warm_connection() -> None:
        reported = False
        try:
            async with engine.connect() as connection:
                await asyncio.wait_for(
                    connection.execute(text("SELECT 1")),
                    timeout=DATABASE_TIMEOUT_SECONDS,
                )
                await outcomes.put(True)
                reported = True
                # Keep every successful checkout until all workers have
                # reported so one connection cannot satisfy the whole warmup.
                await release.wait()
        except Exception:
            if not reported:
                await outcomes.put(False)

    tasks = [
        asyncio.create_task(
            warm_connection(),
            name=f"database-pool-prewarm-{index}",
        )
        for index in range(target)
    ]
    success_count = 0
    try:
        for _ in range(target):
            if await outcomes.get():
                success_count += 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    if success_count != target:
        startup_logger.warning(
            "database pool prewarm incomplete target=%s success=%s "
            "duration_ms=%s",
            target,
            success_count,
            duration_ms,
        )
        return
    startup_logger.info(
        "database pool prewarm complete target=%s success=%s "
        "duration_ms=%s",
        target,
        success_count,
        duration_ms,
    )


@asynccontextmanager
async def app_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    asset_rollout_worker = AssetRolloutWorker(
        session_factory=AsyncSessionLocal,
        concurrency=settings.PROFILE_CONVERGENCE_CONCURRENCY,
        batch_size=settings.PROFILE_CONVERGENCE_BATCH_SIZE,
    )
    application_agent_asset_rollout_worker = ApplicationAgentAssetRolloutWorker(
        session_factory=AsyncSessionLocal,
        batch_size=settings.PROFILE_CONVERGENCE_BATCH_SIZE,
    )
    profile_convergence_worker = ProfileConvergenceWorker(
        session_factory=AsyncSessionLocal,
        worker_id=f"backend-profile-{uuid4()}",
        lease_seconds=settings.PROFILE_CONVERGENCE_LEASE_SECONDS,
        concurrency=settings.PROFILE_CONVERGENCE_CONCURRENCY,
        batch_size=settings.PROFILE_CONVERGENCE_BATCH_SIZE,
    )
    profile_execution_control_worker = ProfileExecutionControlWorker(
        session_factory=AsyncSessionLocal,
        worker_id=f"backend-profile-execution-{uuid4()}",
        lease_seconds=settings.PROFILE_CONVERGENCE_LEASE_SECONDS,
        concurrency=settings.PROFILE_CONVERGENCE_CONCURRENCY,
        batch_size=settings.PROFILE_CONVERGENCE_BATCH_SIZE,
    )
    mcp_registry_worker = RuntimeMcpRegistryConvergenceWorker(
        session_factory=AsyncSessionLocal,
        worker_id=f"backend-mcp-registry-{uuid4()}",
        lease_seconds=settings.MCP_REGISTRY_CONVERGENCE_LEASE_SECONDS,
        concurrency=settings.MCP_REGISTRY_CONVERGENCE_CONCURRENCY,
        batch_size=settings.MCP_REGISTRY_CONVERGENCE_BATCH_SIZE,
    )

    async def converge_profiles_forever() -> None:
        while True:
            try:
                await asset_rollout_worker.reconcile_once()
                await application_agent_asset_rollout_worker.reconcile_once()
                await profile_execution_control_worker.reconcile_once()
                await profile_convergence_worker.reconcile_once()
            except Exception:
                # Durable jobs and leases preserve recovery for the next scan.
                pass
            await asyncio.sleep(settings.PROFILE_CONVERGENCE_POLL_SECONDS)

    async def converge_mcp_registries_forever() -> None:
        while True:
            try:
                await mcp_registry_worker.reconcile_once()
            except Exception:
                pass
            await asyncio.sleep(
                settings.MCP_REGISTRY_CONVERGENCE_POLL_SECONDS
            )

    async def recover_expert_operations_once() -> None:
        try:
            async with AsyncSessionLocal() as db:
                await fail_interrupted_expert_profile_operations(db)
        except Exception:
            # A migration/readiness issue must not take down unrelated APIs.
            # The durable running row remains visible for a later restart/retry.
            pass

    async def maintain_runtime_projections_forever() -> None:
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    await ChatLogProjector(
                        SqlAlchemyChatLogStore(db)
                    ).project_pending_sessions(limit=50)
                    await delete_expired_runtime_projection_events(
                        db,
                        older_than=datetime.now(timezone.utc) - timedelta(seconds=settings.RUNTIME_RUN_EVENT_RETENTION_SECONDS)
                    )
            except Exception:
                # A later iteration retries; request handling must remain available.
                pass
            await asyncio.sleep(settings.RUNTIME_RUN_RECONCILE_INTERVAL_SECONDS)

    async def reconcile_kanban_leases_forever() -> None:
        adapter = build_runtime_protocol_adapters().dashboard_adapter
        reconciler = RuntimeKanbanLeaseReconciler(
            session_factory=AsyncSessionLocal,
            profile_adapter=HermesProfileAdapter(adapter),
            interval_seconds=settings.RUNTIME_KANBAN_LEASE_RECONCILE_INTERVAL_SECONDS,
            release_grace_seconds=settings.RUNTIME_KANBAN_LEASE_RELEASE_GRACE_SECONDS,
        )
        while True:
            try:
                await reconciler.reconcile_once()
            except Exception:
                # Native Board state remains authoritative and the durable
                # lease will be retried by the next iteration.
                pass
            await asyncio.sleep(settings.RUNTIME_KANBAN_LEASE_RECONCILE_INTERVAL_SECONDS)

    async def reconcile_expert_collaborations_forever() -> None:
        adapter = build_runtime_protocol_adapters().dashboard_adapter
        reconciler = ExpertCollaborationReconciler(
            session_factory=AsyncSessionLocal,
            profile_adapter=HermesProfileAdapter(adapter),
            runner=expert_collaboration_runner,
            interval_seconds=(
                settings.EXPERT_COLLABORATION_RECONCILE_INTERVAL_SECONDS
            ),
        )
        while True:
            try:
                await reconciler.reconcile_once()
            except Exception:
                # Collaboration rows and native Board state are durable; the
                # next pass retries without affecting ordinary chat traffic.
                pass
            await asyncio.sleep(
                settings.EXPERT_COLLABORATION_RECONCILE_INTERVAL_SECONDS
            )

    async def process_wecom_events_forever() -> None:
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    event_ids = await queued_wecom_callback_event_ids(db)
                for event_id in event_ids:
                    async with AsyncSessionLocal() as db:
                        await process_wecom_callback_event(
                            db=db,
                            event_id=event_id,
                        )
            except Exception:
                # Durable queued rows remain available for the next scan.
                pass
            await asyncio.sleep(
                settings.APPLICATION_AGENT_WECOM_EVENT_SCAN_INTERVAL_SECONDS
            )

    async def cleanup_expert_release_artifacts_forever() -> None:
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    await cleanup_expired_expert_release_artifacts(db)
            except Exception:
                # Release artifacts are deliberately retained on any cleanup
                # failure.  The next pass can retry without affecting experts.
                pass
            await asyncio.sleep(
                settings.EXPERT_RELEASE_ARTIFACT_CLEANUP_INTERVAL_SECONDS
            )

    if settings.RUNTIME_CHAT_MODE == "real":
        await _prewarm_database_pool()
    expert_recovery_task = (
        asyncio.create_task(recover_expert_operations_once())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    projection_maintenance_task = (
        asyncio.create_task(maintain_runtime_projections_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    runtime_warm_keepalive_task = (
        asyncio.create_task(keep_runtimes_warm_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    profile_convergence_task = (
        asyncio.create_task(converge_profiles_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    mcp_registry_convergence_task = (
        asyncio.create_task(converge_mcp_registries_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    kanban_lease_task = (
        asyncio.create_task(reconcile_kanban_leases_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    expert_collaboration_task = (
        asyncio.create_task(reconcile_expert_collaborations_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    wecom_event_task = (
        asyncio.create_task(process_wecom_events_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    expert_release_cleanup_task = (
        asyncio.create_task(cleanup_expert_release_artifacts_forever())
        if settings.RUNTIME_CHAT_MODE == "real"
        else None
    )
    clarify_scanner = RuntimeClarifyScanner(
        session_factory=AsyncSessionLocal,
        auto_default_seconds=(
            settings.HERMES_USER_WEB_CLARIFY_AUTO_DEFAULT_SECONDS
        ),
        claim_lease_seconds=(
            settings.HERMES_USER_WEB_CLARIFY_CLAIM_LEASE_SECONDS
        ),
        max_auto_resume_attempts=(
            settings.HERMES_USER_WEB_CLARIFY_MAX_AUTO_RESUME_ATTEMPTS
        ),
        batch_size=settings.HERMES_USER_WEB_CLARIFY_SCAN_BATCH_SIZE,
        interval_seconds=(
            settings.HERMES_USER_WEB_CLARIFY_SCAN_INTERVAL_SECONDS
        ),
    )
    if settings.RUNTIME_CHAT_MODE == "real":
        expert_collaboration_runner.start()
        clarify_scanner.start()
    im_gateway.start()
    scheduled_task_scanner = None
    if settings.RUNTIME_CHAT_MODE == "real":
        scheduled_task_launcher.start()
        scheduled_task_scanner = DueTaskScanner(
            scheduled_task_launcher,
            interval_seconds=settings.SCHEDULED_TASK_SCAN_INTERVAL_SECONDS,
        )
    scheduled_task_scanner.start()
    try:
        yield
    finally:
        await im_gateway.stop()
        if scheduled_task_scanner is not None:
            await scheduled_task_scanner.stop()
        await scheduled_task_launcher.stop()
        if projection_maintenance_task is not None:
            projection_maintenance_task.cancel()
            try:
                await projection_maintenance_task
            except asyncio.CancelledError:
                pass
        if runtime_warm_keepalive_task is not None:
            runtime_warm_keepalive_task.cancel()
            try:
                await runtime_warm_keepalive_task
            except asyncio.CancelledError:
                pass
        if profile_convergence_task is not None:
            profile_convergence_task.cancel()
            try:
                await profile_convergence_task
            except asyncio.CancelledError:
                pass
        if mcp_registry_convergence_task is not None:
            mcp_registry_convergence_task.cancel()
            try:
                await mcp_registry_convergence_task
            except asyncio.CancelledError:
                pass
        if kanban_lease_task is not None:
            kanban_lease_task.cancel()
            try:
                await kanban_lease_task
            except asyncio.CancelledError:
                pass
        if expert_collaboration_task is not None:
            expert_collaboration_task.cancel()
            try:
                await expert_collaboration_task
            except asyncio.CancelledError:
                pass
        if wecom_event_task is not None:
            wecom_event_task.cancel()
            try:
                await wecom_event_task
            except asyncio.CancelledError:
                pass
        if expert_release_cleanup_task is not None:
            expert_release_cleanup_task.cancel()
            try:
                await expert_release_cleanup_task
            except asyncio.CancelledError:
                pass
        await clarify_scanner.stop()
        await expert_collaboration_runner.stop()
        if expert_recovery_task is not None:
            expert_recovery_task.cancel()
            try:
                await expert_recovery_task
            except asyncio.CancelledError:
                pass
        await close_runtime_http_client()
        await close_platform_session_store()
        await close_runtime_access_nonce_store()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Hermes Cloud Platform", version="0.1.0", lifespan=app_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "app": "hermes-cloud-platform",
            "env": settings.APP_ENV,
        }

    @app.get("/api/v1/health")
    async def api_health() -> dict[str, str]:
        return {
            "status": "ok",
            "app": "hermes-cloud-platform",
            "env": settings.APP_ENV,
        }

    @app.get("/api/v1/readyz")
    async def api_readiness() -> JSONResponse:
        payload = await get_platform_readiness()
        return JSONResponse(
            status_code=200 if payload["status"] == "ready" else 503,
            content=payload,
        )

    @app.get("/api/v1/health/im-gateway")
    async def im_gateway_health() -> dict[str, object]:
        return im_gateway.health()

    app.include_router(runtime_workspace_router, prefix="/api/v1/user")
    app.include_router(
        application_agent_runtime_knowledge_router,
        prefix="/api/v1",
    )
    app.include_router(
        application_agent_runtime_personal_knowledge_router,
        prefix="/api/v1",
    )
    app.include_router(openapi_application_agents_router, prefix="/openapi/v1")
    app.include_router(desktop_updates_router, prefix="/api/v1")
    app.include_router(app_center_content_router, prefix="/api/v1")
    app.include_router(wecom_channel_router)
    app.include_router(runtime_models_router, prefix="/api/v1/user")
    app.include_router(runtime_capabilities_router, prefix="/api/v1/user")
    app.include_router(runtime_sessions_router, prefix="/api/v1/user")
    app.include_router(user_runtime_clarify_router, prefix="/api/v1/user")
    app.include_router(scheduled_tasks_router, prefix="/api/v1/user")
    app.include_router(user_chat_router, prefix="/api/v1/user")
    app.include_router(runtime_edge_router, prefix="/api/v1/user")
    app.include_router(user_browser_presence_router, prefix="/api/v1/user")
    app.include_router(user_avatar_assets_router, prefix="/api/v1/user")
    app.include_router(user_application_agents_router, prefix="/api/v1/user")
    app.include_router(
        user_application_agent_im_channels_router,
        prefix="/api/v1/user",
    )
    app.include_router(
        user_application_agent_wecom_router,
        prefix="/api/v1/user",
    )
    app.include_router(
        user_application_agent_chat_files_router,
        prefix="/api/v1/user",
    )
    app.include_router(user_chat_files_router, prefix="/api/v1/user")
    app.include_router(user_apps_router, prefix="/api/v1/user")
    app.include_router(user_data_app_launch_router, prefix="/api/v1/user")
    app.include_router(user_data_apps_router, prefix="/api/v1/user")
    app.include_router(user_enterprise_knowledge_router, prefix="/api/v1/user")
    app.include_router(user_experts_router, prefix="/api/v1/user")
    app.include_router(default_profile_assets_router, prefix="/api/v1/user")
    app.include_router(user_im_channels_router, prefix="/api/v1/user")
    app.include_router(user_personal_knowledge_router, prefix="/api/v1/user")
    app.include_router(user_personal_skills_router, prefix="/api/v1/user")
    app.include_router(user_directory_router, prefix="/api/v1/user")
    app.include_router(user_task_prompt_config_router, prefix="/api/v1/user")
    app.include_router(user_wecom_bind_router, prefix="/api/v1/user")
    app.include_router(admin_experts_router, prefix="/api/v1/admin")
    app.include_router(admin_asset_rollouts_router, prefix="/api/v1/admin")
    app.include_router(admin_assets_router, prefix="/api/v1/admin")
    app.include_router(admin_audit_logs_router, prefix="/api/v1/admin")
    app.include_router(admin_runtime_admission_audits_router, prefix="/api/v1/admin")
    app.include_router(admin_application_agent_admins_router, prefix="/api/v1/admin")
    app.include_router(admin_me_router, prefix="/api/v1/admin")
    app.include_router(admin_models_router, prefix="/api/v1/admin")
    app.include_router(admin_model_config_router, prefix="/api/v1/admin")
    app.include_router(admin_kubernetes_resources_router, prefix="/api/v1/admin")
    app.include_router(admin_knowledge_governance_router, prefix="/api/v1/admin")
    app.include_router(admin_knowledge_providers_router, prefix="/api/v1/admin")
    app.include_router(admin_resources_router, prefix="/api/v1/admin")
    app.include_router(admin_usage_router, prefix="/api/v1/admin")
    app.include_router(admin_task_prompt_config_router, prefix="/api/v1/admin")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(oauth2_router, prefix="/api/v1")
    app.include_router(runtime_knowledge_router, prefix="/api/v1")
    app.include_router(runtime_personal_knowledge_router, prefix="/api/v1")
    app.include_router(runtime_kanban_router, prefix="/api/v1")
    app.include_router(runtime_clarify_router, prefix="/api/v1")
    app.include_router(runtime_experts_router, prefix="/api/v1")
    app.include_router(runtime_config_bundles_router, prefix="/internal")
    app.include_router(profile_artifacts_router, prefix="/internal")
    app.include_router(runtime_projections_router, prefix="/internal")
    app.include_router(runtime_admission_audits_router, prefix="/internal")
    app.include_router(runtime_edge_auth_router, prefix="/internal")

    return app


app = create_app()
install_oauth_access_log_filter()
