from __future__ import annotations

from dataclasses import replace
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal, get_db
from app.api.v1.auth import get_auth_token_service
from app.hermes_runtime.providers.knative import KubectlKnativeClient
from app.schemas.runtime_workspace import (
    RuntimeStartupContextResponse,
    RuntimeWorkspaceSummary,
)
from app.services.runtime_admission import RuntimeAdmissionError
from app.services.runtime_infrastructure import (
    RuntimeInfrastructure,
    build_runtime_infrastructure,
)
from app.services.runtime_workspace import (
    Principal,
    RuntimeBindingEnsureResult,
    RuntimeKeepaliveError,
    RuntimeWarmupRunner,
    RuntimeWorkspaceRecord,
    RuntimeWorkspaceService,
    RuntimeBindingStatusPatch,
    SqlAlchemyRuntimeBindingStatusStore,
    RuntimeSkillPolicyConflictError,
    RuntimeWorkspaceNotFoundError,
)
from app.hermes_runtime.skill_visibility_policy import (
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
)
from app.schemas.runtime_workspace import RuntimeSkillPolicyUpdateRequest, RuntimeSkillPolicyUpdateResponse
from app.services.auth import AuthTokenService, AuthenticationError
from app.services.runtime_startup_context import RuntimeStartupContextService

router = APIRouter(prefix="/runtime-workspace", tags=["user-runtime-workspace"])
_ADMIN_CONTROLLED_RUNTIME_STATUSES = {
    "force_stopped",
    "force_stop_requested",
    "force_stop_failed",
    "restart_requested",
}
_bootstrap_inflight: set[int] = set()
logger = logging.getLogger(__name__)


def configured_dev_principal() -> Principal:
    user_id = settings.DEV_PRINCIPAL_USER_ID
    if settings.RUNTIME_CHAT_MODE == "real" and user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DEV_PRINCIPAL_USER_ID must identify an existing user in real Runtime mode",
        )
    return Principal(user_id=user_id, org_id=settings.DEFAULT_ORG_ID)


class InMemoryRuntimeWorkspaceStore:
    def __init__(self) -> None:
        self.profile: RuntimeWorkspaceRecord | None = None
        self.binding_ensured = False

    async def get_default_profile(self, *, org_id: int, user_id: int) -> RuntimeWorkspaceRecord | None:
        if self.profile and self.profile.org_id == org_id and self.profile.user_id == user_id:
            return self.profile
        return None

    async def create_default_profile(
        self,
        *,
        org_id: int,
        user_id: int,
        name: str,
    ) -> RuntimeWorkspaceRecord:
        self.profile = RuntimeWorkspaceRecord(
            runtime_workspace_id=1,
            cloud_profile_id=1,
            org_id=org_id,
            user_id=user_id,
            user_name="测试用户",
            user_code=f"employee-{user_id}",
            name=name,
            status="active",
            runtime_status="absent",
            config_generation=1,
        )
        return self.profile

    async def ensure_binding(self, record: RuntimeWorkspaceRecord) -> RuntimeBindingEnsureResult:
        self.binding_ensured = True
        return RuntimeBindingEnsureResult(
            plan=RuntimeWorkspaceService.build_default_binding_plan(record),
            api_server_key="contract-runtime-api-key",
            created=False,
            reconcile_required=False,
        )

    async def get_runtime_endpoint(self, record: RuntimeWorkspaceRecord):
        from app.hermes_runtime.schemas import RuntimeEndpoint

        return RuntimeEndpoint(
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_id=f"rt-{record.runtime_workspace_id}",
            endpoint_base_url="http://runtime.example",
            endpoint_host_header="runtime.example",
            api_key="contract-runtime-api-key",
        )

    async def update_skill_policy(self, *, principal, policy_version: int, expected_config_generation: int):
        if policy_version not in {SKILL_VISIBILITY_POLICY_V1, SKILL_VISIBILITY_POLICY_V2, SKILL_VISIBILITY_POLICY_V3}:
            raise ValueError("unsupported Skill visibility policy")
        if self.profile is None or self.profile.org_id != principal.org_id or self.profile.user_id != principal.user_id:
            raise RuntimeWorkspaceNotFoundError("runtime workspace not found")
        if self.profile.config_generation != expected_config_generation:
            raise RuntimeSkillPolicyConflictError(RuntimeSkillPolicyConflictError.code)
        self.profile = replace(
            self.profile,
            config_generation=expected_config_generation + 1,
            pending_reload=True,
        )
        from app.services.runtime_workspace import SkillPolicyUpdateResult
        return SkillPolicyUpdateResult(
            runtime_workspace_id=self.profile.runtime_workspace_id,
            policy_version=policy_version,
            config_generation=self.profile.config_generation,
            pending_reload=True,
        )


class InMemoryRuntimeWarmupRunner:
    async def warmup(self, record: RuntimeWorkspaceRecord) -> RuntimeWorkspaceRecord:
        return RuntimeWorkspaceRecord(
            runtime_workspace_id=record.runtime_workspace_id,
            cloud_profile_id=record.cloud_profile_id,
            org_id=record.org_id,
            user_id=record.user_id,
            user_name=record.user_name,
            user_code=record.user_code,
            name=record.name,
            status=record.status,
            runtime_status="ready",
            pending_reload=False,
            config_generation=record.config_generation,
        )


_store = InMemoryRuntimeWorkspaceStore()


async def get_principal(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    auth_service: AuthTokenService = Depends(get_auth_token_service),
) -> Principal:
    if settings.HERMES_ADMIN_AUTH_MODE == "dev" and request.url.path.startswith("/api/v1/admin"):
        return configured_dev_principal()
    if authorization:
        try:
            return await auth_service.principal_from_bearer(authorization)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if settings.HERMES_AUTH_MODE == "dev":
        return configured_dev_principal()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")


async def get_runtime_infrastructure(
    db: AsyncSession = Depends(get_db),
) -> RuntimeInfrastructure | None:
    if settings.RUNTIME_CHAT_MODE == "real":
        return build_runtime_infrastructure(db)
    return None


async def get_runtime_workspace_service(
    infrastructure: RuntimeInfrastructure | None = Depends(
        get_runtime_infrastructure
    ),
) -> RuntimeWorkspaceService:
    if infrastructure is not None:
        return infrastructure.workspace_service
    return RuntimeWorkspaceService(_store)


async def get_runtime_warmup_runner(
    infrastructure: RuntimeInfrastructure | None = Depends(
        get_runtime_infrastructure
    ),
) -> RuntimeWarmupRunner:
    if infrastructure is not None:
        return infrastructure.warmup_runner
    return InMemoryRuntimeWarmupRunner()


async def _run_real_runtime_bootstrap(
    principal: Principal,
    runtime_workspace_id: int,
    binding: RuntimeBindingEnsureResult,
) -> None:
    """Run the slow first-Binding/KService path after bootstrap responds."""
    try:
        async with AsyncSessionLocal() as session:
            infrastructure = build_runtime_infrastructure(session)
            service = infrastructure.workspace_service
            profile = await service.ensure_default_for_user(principal)
            if profile.runtime_workspace_id != runtime_workspace_id:
                return
            if profile.runtime_status in _ADMIN_CONTROLLED_RUNTIME_STATUSES:
                return

            steady_login_activation = (
                profile.runtime_status == "ready"
                and not profile.pending_reload
            )
            if steady_login_activation:
                try:
                    ready_record = await infrastructure.warmup_runner.warmup_bound(
                        profile,
                        binding,
                    )
                    if ready_record.runtime_status != "ready":
                        return
                    endpoint = await infrastructure.workspace_store.get_runtime_endpoint(
                        ready_record
                    )
                    await infrastructure.activator.activate_dashboard(endpoint)
                except Exception:
                    logger.exception(
                        "steady Runtime login activation skipped workspace_id=%s",
                        runtime_workspace_id,
                    )
                return

            ready_record = await infrastructure.warmup_runner.warmup_bound(
                profile,
                binding,
            )
            if ready_record.runtime_status != "ready":
                return
            if profile.pending_reload and binding.plan.binding_id:
                from app.services.runtime_reload import RuntimeReloadService

                await RuntimeReloadService(session).requeue_personal_projections(
                    binding_id=int(binding.plan.binding_id),
                )
                await session.commit()

    except Exception as exc:
        async with AsyncSessionLocal() as session:
            status_store = SqlAlchemyRuntimeBindingStatusStore(session)
            await status_store.update_binding_status(
                RuntimeBindingStatusPatch(
                    runtime_workspace_id=runtime_workspace_id,
                    knative_status_url=None,
                    route_host=None,
                    endpoint_base_url=None,
                    endpoint_host_header=None,
                    status="failed",
                    last_error=f"runtime bootstrap failed: {str(exc)[:300]}",
                )
            )
    finally:
        _bootstrap_inflight.discard(runtime_workspace_id)


def _summary(profile: RuntimeWorkspaceRecord) -> RuntimeWorkspaceSummary:
    return RuntimeWorkspaceSummary(
        runtime_workspace_id=profile.runtime_workspace_id,
        cloud_profile_id=profile.cloud_profile_id,
        org_id=profile.org_id,
        user_id=profile.user_id,
        name=profile.name,
        status=profile.status,
        default_model_id=profile.default_model_id,
        runtime_status=profile.runtime_status,
        pending_reload=profile.pending_reload,
        config_generation=profile.config_generation,
    )


@router.get("", response_model=RuntimeWorkspaceSummary)
async def get_runtime_workspace(
    principal: Principal = Depends(get_principal),
    service: RuntimeWorkspaceService = Depends(get_runtime_workspace_service),
) -> RuntimeWorkspaceSummary:
    profile = await service.ensure_default_for_user(principal)
    return _summary(profile)


@router.put("/skill-policy", response_model=RuntimeSkillPolicyUpdateResponse)
async def update_runtime_skill_policy(
    payload: RuntimeSkillPolicyUpdateRequest,
    principal: Principal = Depends(get_principal),
    service: RuntimeWorkspaceService = Depends(get_runtime_workspace_service),
) -> RuntimeSkillPolicyUpdateResponse:
    try:
        result = await service.update_skill_policy(
            principal=principal,
            policy_version=payload.policy_version,
            expected_config_generation=payload.expected_config_generation,
        )
    except RuntimeSkillPolicyConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except RuntimeWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RuntimeSkillPolicyUpdateResponse(
        runtime_workspace_id=result.runtime_workspace_id,
        policy_version=result.policy_version,
        config_generation=result.config_generation,
        pending_reload=result.pending_reload,
    )


@router.post("/warmup", response_model=RuntimeWorkspaceSummary)
async def warmup_runtime_workspace(
    principal: Principal = Depends(get_principal),
    service: RuntimeWorkspaceService = Depends(get_runtime_workspace_service),
    warmup_runner: RuntimeWarmupRunner = Depends(get_runtime_warmup_runner),
) -> RuntimeWorkspaceSummary:
    profile = await service.ensure_default_for_user(principal)
    if profile.runtime_status in _ADMIN_CONTROLLED_RUNTIME_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="runtime workspace is under administrator control",
        )
    try:
        profile = await warmup_runner.warmup(profile)
    except RuntimeAdmissionError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.decision.retryable
                else status.HTTP_409_CONFLICT
            ),
            detail=exc.decision.code,
        ) from exc
    return _summary(profile)


@router.post("/bootstrap", response_model=RuntimeWorkspaceSummary)
async def bootstrap_runtime_workspace(
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(get_principal),
    service: RuntimeWorkspaceService = Depends(get_runtime_workspace_service),
    db: AsyncSession = Depends(get_db),
) -> RuntimeWorkspaceSummary:
    """Ensure a user's directory/binding and start KService provisioning asynchronously."""
    profile = await service.ensure_default_for_user(principal)
    if profile.runtime_status in _ADMIN_CONTROLLED_RUNTIME_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="runtime workspace is under administrator control",
        )
    if settings.RUNTIME_CHAT_MODE != "real":
        return _summary(await service.warmup_default_for_user(principal, InMemoryRuntimeWarmupRunner()))
    # ready 但 pending_reload（如身份代数推进）同样需要调度后台重投影；
    # 名义 ready 也不能跳过缩零激活。
    runtime_workspace_id = profile.runtime_workspace_id
    if runtime_workspace_id in _bootstrap_inflight:
        return _summary(profile)
    _bootstrap_inflight.add(runtime_workspace_id)
    task_scheduled = False
    try:
        # Persist the first Binding before responding, but retain the
        # ownership result across the BackgroundTasks boundary.
        store = getattr(service, "store", None)
        ensure_binding = getattr(store, "ensure_binding", None)
        if not callable(ensure_binding):
            raise RuntimeError("real Runtime bootstrap store cannot ensure a binding")
        binding = await ensure_binding(profile)
        if (
            binding.binding_status == "starting"
            and not binding.created
            and not binding.initial_bootstrap_resume_eligible
        ):
            return _summary(profile)
        if binding.initial_bootstrap_resume_eligible:
            binding = replace(binding, reconcile_owned=True)
        profile = await service.ensure_default_for_user(principal)
        background_tasks.add_task(
            _run_real_runtime_bootstrap,
            principal,
            runtime_workspace_id,
            binding,
        )
        task_scheduled = True
    finally:
        if not task_scheduled:
            _bootstrap_inflight.discard(runtime_workspace_id)
    if binding.binding_status == "failed":
        profile = replace(
            profile,
            runtime_status="starting",
            pending_reload=True,
        )
    return _summary(profile)


@router.get("/startup-context", response_model=RuntimeStartupContextResponse)
async def get_runtime_startup_context(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeStartupContextResponse:
    k8s_client = KubectlKnativeClient(kubeconfig=settings.KUBECONFIG or None)
    result = await RuntimeStartupContextService(
        db,
        pod_lister=k8s_client,
        namespace=settings.HERMES_RUNTIME_NAMESPACE,
    ).resolve(principal)
    return RuntimeStartupContextResponse(
        runtime_workspace_id=result.runtime_workspace_id,
        context=result.context,
    )


@router.post("/keepalive", status_code=status.HTTP_204_NO_CONTENT)
async def keepalive_runtime_workspace(
    principal: Principal = Depends(get_principal),
    service: RuntimeWorkspaceService = Depends(get_runtime_workspace_service),
) -> Response:
    try:
        await service.keepalive_default_for_user(principal)
    except RuntimeKeepaliveError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime is not ready",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
