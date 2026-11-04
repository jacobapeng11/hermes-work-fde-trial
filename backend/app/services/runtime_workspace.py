from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Awaitable, Callable, Protocol

from sqlalchemy import insert, select
from app.config import settings
from sqlalchemy.ext.asyncio import AsyncSession

from app.hermes_runtime.providers.knative import (
    KubectlCommandError,
    KnativeRuntimeProvider,
    KnativeRuntimeSpec,
)
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.skill_visibility_policy import (
    SKILL_POLICY_CONFIG_KEY,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
)
from app.models import (
    CloudHermesProfile,
    HermesRuntimeBinding,
    RuntimeAgentProfile,
    RuntimeBootstrapIntent,
    User,
)
from app.repositories.runtime import RuntimeWorkspaceRepository
from app.services.runtime_config_bootstrap import (
    RuntimeConfigPreflightError,
)
from app.services.runtime_agent_profiles import SqlAlchemyRuntimeAgentProfileStore
from app.services.runtime_image_contract import resolve_runtime_image_contract
from app.services.runtime_image_config import (
    RuntimeImageConfig,
    RuntimeImageConfigReader,
    StaticRuntimeImageConfigReader,
    shared_runtime_image_config_store,
)
from app.services.runtime_maintenance import RuntimeMaintenanceService
from app.services.runtime_user_memory import RuntimeUserMemoryWriter
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyMaterial,
    RuntimePlatformControlKeyStore,
    SqlAlchemyRuntimePlatformControlKeyStore,
    bind_persisted_pvc_identity,
    platform_control_secret_name,
    runtime_pvc_storage_identity_hash,
)


@dataclass(frozen=True)
class Principal:
    user_id: int
    org_id: int


@dataclass(frozen=True)
class RuntimeWorkspaceRecord:
    runtime_workspace_id: int
    cloud_profile_id: int
    org_id: int
    user_id: int
    user_name: str
    user_code: str
    name: str
    status: str
    default_model_id: int | None = None
    runtime_status: str | None = None
    pending_reload: bool = False
    config_generation: int = 0


@dataclass(frozen=True)
class SkillPolicyUpdateResult:
    runtime_workspace_id: int
    policy_version: int
    config_generation: int
    pending_reload: bool


class RuntimeSkillPolicyConflictError(RuntimeError):
    code = "skill_policy_generation_conflict"


@dataclass(frozen=True)
class RuntimeBindingPlan:
    org_id: int
    user_id: int
    user_name: str
    user_code: str
    runtime_workspace_id: int
    runtime_id: str
    service_name: str
    secret_name: str
    hermes_home_sub_path: str
    workspace_sub_path: str
    runtime_root_pvc_name: str
    runtime_root_host_path: str
    binding_id: str = ""
    config_revision: str = "0"
    runtime_config_source: str = "pvc"
    image: str = ""
    image_digest: str | None = None
    upstream_commit: str = ""
    state_schema: int = 0
    config_schema: int = 0
    platform_control_secret_name: str = ""
    platform_control_key_version: int = 0
    runtime_root_pvc_uid: str | None = None
    pvc_storage_identity_hash: str | None = None


@dataclass(frozen=True)
class RuntimeBindingEnsureResult:
    plan: RuntimeBindingPlan
    api_server_key: str
    created: bool = False
    reconcile_owned: bool = False
    initial_bootstrap_resume_eligible: bool = False
    reconcile_required: bool = True
    binding_status: str = "starting"
    platform_control_key: RuntimePlatformControlKeyMaterial | None = None


@dataclass(frozen=True)
class RuntimeBindingStatusPatch:
    runtime_workspace_id: int
    knative_status_url: str | None
    route_host: str | None
    endpoint_base_url: str | None
    endpoint_host_header: str | None
    status: str
    last_error: str | None = None


class RuntimeWorkspaceNotFoundError(LookupError):
    pass


class RuntimeKeepaliveError(RuntimeError):
    pass


RUNTIME_RESOURCE_KEY_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def runtime_resource_key(user_code: str) -> str:
    value = user_code.strip().lower()
    if not value or len(value) > 32 or RUNTIME_RESOURCE_KEY_RE.fullmatch(value) is None:
        raise ValueError("invalid runtime resource key")
    return value


def runtime_root_host_path(
    *,
    runtime_root_path: str,
    org_id: int,
    resource_key: str,
) -> str:
    boundary = Path(runtime_root_path).resolve(strict=False)
    candidate = (boundary / str(org_id) / resource_key).resolve(strict=False)
    if not candidate.is_relative_to(boundary):
        raise ValueError("runtime root escapes configured boundary")
    return str(candidate)


class RuntimeWorkspaceStore(Protocol):
    async def get_default_profile(
        self,
        *,
        org_id: int,
        user_id: int,
    ) -> RuntimeWorkspaceRecord | None: ...

    async def create_default_profile(
        self,
        *,
        org_id: int,
        user_id: int,
        name: str,
    ) -> RuntimeWorkspaceRecord: ...

    async def ensure_binding(
        self, record: RuntimeWorkspaceRecord
    ) -> RuntimeBindingEnsureResult: ...

    async def get_runtime_endpoint(self, record: RuntimeWorkspaceRecord) -> RuntimeEndpoint: ...

    async def update_skill_policy(
        self, *, principal: Principal, policy_version: int, expected_config_generation: int
    ) -> SkillPolicyUpdateResult: ...


class RuntimeApiKeyCodec:
    prefix = "local-v1:"

    def encrypt(self, api_key: str) -> str:
        payload = base64.urlsafe_b64encode(api_key.encode("utf-8")).decode("ascii")
        return f"{self.prefix}{payload}"

    def decrypt(self, encrypted: str) -> str:
        if not encrypted.startswith(self.prefix):
            return encrypted
        payload = encrypted[len(self.prefix) :]
        return base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")


class SqlAlchemyRuntimeWorkspaceStore:
    def __init__(
        self,
        session: AsyncSession,
        *,
        namespace: str,
        image: str | None = None,
        runtime_image_config: RuntimeImageConfigReader | None = None,
        repository_root: Path | None = None,
        hermes_home_mount_path: str,
        workspace_mount_path: str,
        ingress_base_url: str,
        api_key_codec: RuntimeApiKeyCodec | None = None,
        platform_control_key_store: RuntimePlatformControlKeyStore | None = None,
    ) -> None:
        self.session = session
        self.namespace = namespace
        if runtime_image_config is not None:
            self.runtime_image_config = runtime_image_config
        elif image is not None:
            self.runtime_image_config = StaticRuntimeImageConfigReader(
                image=image,
            )
        else:
            self.runtime_image_config = shared_runtime_image_config_store()
        self.repository_root = repository_root or Path(__file__).resolve().parents[3]
        self.ingress_base_url = ingress_base_url
        self.hermes_home_mount_path = hermes_home_mount_path
        self.workspace_mount_path = workspace_mount_path
        self.api_key_codec = api_key_codec or RuntimeApiKeyCodec()
        self.platform_control_key_store = (
            platform_control_key_store or SqlAlchemyRuntimePlatformControlKeyStore(self.session)
        )

    async def get_default_profile(
        self,
        *,
        org_id: int,
        user_id: int,
    ) -> RuntimeWorkspaceRecord | None:
        result = await self.session.execute(
            RuntimeWorkspaceRepository.default_profile_statement(org_id=org_id, user_id=user_id)
        )
        # A historical database may contain duplicate profiles created before
        # the owner/type uniqueness migration.  Prefer the oldest canonical
        # workspace instead of turning a login into a 500.
        profile = result.scalars().first()
        if profile is None:
            return None
        return await self._record_from_profile(profile)

    async def create_default_profile(
        self,
        *,
        org_id: int,
        user_id: int,
        name: str,
    ) -> RuntimeWorkspaceRecord:
        statement = (
            insert(CloudHermesProfile)
            .values(
                org_id=org_id,
                user_id=user_id,
                profile_type="default_chat_agent",
                name=name,
                status="active",
                profile_config=(
                    RuntimeWorkspaceRepository.initial_enterprise_knowledge_config()
                ),
            )
            .prefix_with("IGNORE")
        )
        await self.session.execute(statement)
        await self.session.commit()
        profile = (
            (
                await self.session.execute(
                    RuntimeWorkspaceRepository.default_profile_statement(
                        org_id=org_id, user_id=user_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if profile is None:
            raise RuntimeError("failed to create default runtime workspace")
        return await self._record_from_profile(profile)

    async def ensure_binding(self, record: RuntimeWorkspaceRecord) -> RuntimeBindingEnsureResult:
        result = await self.session.execute(
            RuntimeWorkspaceRepository.binding_by_workspace_statement(
                runtime_workspace_id=record.runtime_workspace_id,
            )
        )
        binding = result.scalar_one_or_none()
        if binding is not None:
            plan = self._plan_from_binding(record, binding)
            key_provision = await self.platform_control_key_store.ensure_active_key(
                binding_type="personal_runtime",
                binding_id=int(binding.id),
                runtime_id=str(binding.runtime_id),
            )
            if key_provision.created:
                await self.session.commit()
            (
                selected_image,
                selected_image_digest,
                upstream_commit,
                state_schema,
                config_schema,
            ) = await self._desired_runtime_contract(binding=binding)
            plan = replace(
                plan,
                image=selected_image,
                image_digest=selected_image_digest,
                upstream_commit=upstream_commit,
                state_schema=state_schema,
                config_schema=config_schema,
                platform_control_secret_name=platform_control_secret_name(
                    str(binding.knative_service)
                ),
                platform_control_key_version=(key_provision.material.key_version),
            )
            reconcile_required = not (
                str(binding.status) == "ready"
                and bool(binding.endpoint_base_url)
                and bool(binding.endpoint_host_header)
                and bool(binding.runtime_root_pvc_uid)
                and bool(binding.pvc_storage_identity_hash)
                and not key_provision.created
            )
            # pending Desired（含 identity scope）必须收敛：ready 但 pending 的
            # Binding 不能直接返回 ready，需要走后台重投影完成新代数切换。
            reconcile_required = bool(binding.pending_reload) or reconcile_required
            return RuntimeBindingEnsureResult(
                plan=plan,
                api_server_key=self.api_key_codec.decrypt(binding.api_key_encrypted),
                created=False,
                reconcile_owned=False,
                initial_bootstrap_resume_eligible=(
                    str(binding.status) == "starting"
                    and not bool(binding.endpoint_base_url)
                    and not bool(binding.endpoint_host_header)
                ),
                reconcile_required=reconcile_required,
                binding_status=str(binding.status),
                platform_control_key=key_provision.material,
            )

        plan = RuntimeWorkspaceService.build_default_binding_plan(record)
        (
            desired_image,
            desired_image_digest,
            upstream_commit,
            state_schema,
            config_schema,
        ) = await self._desired_runtime_contract(binding=None)
        plan = replace(
            plan,
            image=desired_image,
            image_digest=desired_image_digest,
            upstream_commit=upstream_commit,
            state_schema=state_schema,
            config_schema=config_schema,
        )
        occupied_result = await self.session.execute(
            RuntimeWorkspaceRepository.binding_by_service_statement(
                namespace=self.namespace,
                service_name=plan.service_name,
            )
        )
        if occupied_result.scalars().first() is not None:
            raise RuntimeConfigPreflightError("runtime resource name is already bound")
        api_server_key = secrets.token_urlsafe(32)
        await self.session.execute(
            RuntimeWorkspaceRepository.create_runtime_binding_statement(
                org_id=record.org_id,
                user_id=record.user_id,
                runtime_workspace_id=record.runtime_workspace_id,
                runtime_id=plan.runtime_id,
                service_name=plan.service_name,
                namespace=self.namespace,
                image=desired_image,
                image_digest=desired_image_digest,
                api_key_encrypted=self.api_key_codec.encrypt(api_server_key),
                runtime_root_pvc_name=plan.runtime_root_pvc_name,
                runtime_root_host_path=plan.runtime_root_host_path,
                hermes_home_sub_path=plan.hermes_home_sub_path,
                workspace_sub_path=plan.workspace_sub_path,
                container_mounts={
                    "hermes_home": self.hermes_home_mount_path,
                    "workspace": self.workspace_mount_path,
                },
            )
        )
        await self.session.commit()
        inserted = (
            await self.session.execute(
                RuntimeWorkspaceRepository.binding_by_workspace_statement(
                    runtime_workspace_id=record.runtime_workspace_id,
                )
            )
        ).scalar_one()
        inserted_id = int(inserted.id)
        key_provision = await self.platform_control_key_store.ensure_active_key(
            binding_type="personal_runtime",
            binding_id=inserted_id,
            runtime_id=plan.runtime_id,
        )
        await self.session.commit()
        plan = replace(
            plan,
            binding_id=str(inserted_id),
            config_revision=(
                f"generation-{int(getattr(inserted, 'config_generation', 1) or 1)}"
            ),
            platform_control_secret_name=platform_control_secret_name(plan.service_name),
            platform_control_key_version=key_provision.material.key_version,
        )
        return RuntimeBindingEnsureResult(
            plan=plan,
            api_server_key=api_server_key,
            created=True,
            reconcile_owned=True,
            reconcile_required=True,
            binding_status="starting",
            platform_control_key=key_provision.material,
        )

    async def prepare_reload(
        self,
        *,
        runtime_id: str,
        target: RuntimeImageConfig,
        maintenance_epoch: str | None = None,
    ) -> tuple[RuntimeWorkspaceRecord, RuntimeBindingEnsureResult]:
        if not target.config_id or not target.manifest_digest:
            raise RuntimeConfigPreflightError("Runtime reload target identity is incomplete")
        maintenance = await RuntimeMaintenanceService(self.session).snapshot(lock=True)
        if maintenance_epoch is None:
            if maintenance.maintenance_active:
                raise RuntimeConfigPreflightError("global Runtime maintenance is frozen")
        elif (
            not maintenance.maintenance_active
            or maintenance.maintenance_epoch != maintenance_epoch
        ):
            raise RuntimeConfigPreflightError("Runtime maintenance ownership does not match")
        binding = await self.session.scalar(
            select(HermesRuntimeBinding)
            .where(HermesRuntimeBinding.runtime_id == runtime_id)
            .with_for_update()
        )
        if binding is None:
            raise RuntimeWorkspaceNotFoundError("personal Runtime Binding not found")
        if not binding.runtime_root_pvc_uid or not binding.pvc_storage_identity_hash:
            raise RuntimeConfigPreflightError("Runtime Binding PVC identity is incomplete")
        profile = await self.session.scalar(
            select(CloudHermesProfile).where(
                CloudHermesProfile.id == int(binding.runtime_workspace_id)
            )
        )
        user = await self.session.scalar(
            select(User).where(
                User.id == int(binding.user_id),
                User.org_id == int(binding.org_id),
                User.status == "active",
            )
        )
        if profile is None or user is None or not str(user.user_code or "").strip():
            raise RuntimeConfigPreflightError("Runtime owner identity is incomplete")
        contract = resolve_runtime_image_contract(
            repository_root=self.repository_root,
            image_reference=target.image,
        )
        key_provision = await self.platform_control_key_store.ensure_active_key(
            binding_type="personal_runtime",
            binding_id=int(binding.id),
            runtime_id=str(binding.runtime_id),
        )
        binding.image = target.image
        binding.image_digest = target.config_id
        binding.status = "updating"
        binding.pending_reload = True
        binding.last_error = None
        await self.session.commit()
        record = RuntimeWorkspaceRecord(
            runtime_workspace_id=int(profile.id),
            cloud_profile_id=int(profile.id),
            org_id=int(binding.org_id),
            user_id=int(binding.user_id),
            user_name=str(user.name),
            user_code=str(user.user_code),
            name=str(profile.name),
            status=str(profile.status),
            default_model_id=(
                int(profile.default_model_id) if profile.default_model_id else None
            ),
            runtime_status="updating",
            pending_reload=True,
        )
        plan = replace(
            self._plan_from_binding(record, binding),
            image=target.image,
            image_digest=target.config_id,
            upstream_commit=contract.upstream_commit,
            state_schema=contract.state_schema,
            config_schema=contract.config_schema,
            platform_control_secret_name=platform_control_secret_name(
                str(binding.knative_service)
            ),
            platform_control_key_version=key_provision.material.key_version,
        )
        return record, RuntimeBindingEnsureResult(
            plan=plan,
            api_server_key=self.api_key_codec.decrypt(binding.api_key_encrypted),
            created=False,
            reconcile_owned=True,
            reconcile_required=True,
            binding_status="updating",
            platform_control_key=key_provision.material,
        )

    async def _desired_runtime_contract(
        self,
        *,
        binding: HermesRuntimeBinding | None,
    ) -> tuple[str, str | None, str, int, int]:
        reuse_configured_image = binding is not None and await self._is_uncommitted_initial_failure(
            binding
        )
        if (
            binding is not None
            and not reuse_configured_image
            and str(getattr(binding, "image", "")).strip()
        ):
            image = str(binding.image)
            image_digest = str(getattr(binding, "image_digest", "") or "").strip() or None
        else:
            configured = self.runtime_image_config.read()
            image = configured.image
            image_digest = configured.config_id
        contract = resolve_runtime_image_contract(
            repository_root=self.repository_root,
            image_reference=image,
        )
        return (
            image,
            image_digest,
            contract.upstream_commit,
            contract.state_schema,
            contract.config_schema,
        )

    async def _is_uncommitted_initial_failure(
        self,
        binding: HermesRuntimeBinding,
    ) -> bool:
        if not (
            str(getattr(binding, "status", "")) == "failed"
            and not getattr(binding, "endpoint_base_url", None)
            and not getattr(binding, "endpoint_host_header", None)
            and not getattr(binding, "image_digest", None)
        ):
            return False
        intent_id = (
            await self.session.execute(
                select(RuntimeBootstrapIntent.id)
                .where(
                    RuntimeBootstrapIntent.runtime_binding_type == "personal_runtime",
                    RuntimeBootstrapIntent.runtime_binding_id == int(binding.id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return intent_id is None

    async def get_runtime_endpoint(self, record: RuntimeWorkspaceRecord) -> RuntimeEndpoint:
        result = await self.session.execute(
            RuntimeWorkspaceRepository.binding_by_workspace_statement(
                runtime_workspace_id=record.runtime_workspace_id,
            )
        )
        binding = result.scalar_one_or_none()
        if (
            binding is None
            or binding.org_id != record.org_id
            or binding.user_id != record.user_id
            or binding.status != "ready"
            or not binding.endpoint_base_url
            or not binding.endpoint_host_header
        ):
            raise RuntimeKeepaliveError("runtime is not ready")
        return RuntimeEndpoint(
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_id=str(binding.runtime_id),
            endpoint_base_url=str(binding.endpoint_base_url),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_key=self.api_key_codec.decrypt(str(binding.api_key_encrypted)),
        )

    async def _record_from_profile(self, profile: CloudHermesProfile) -> RuntimeWorkspaceRecord:
        await self._ensure_default_agent_profile(profile)
        user = (
            await self.session.execute(
                select(User).where(
                    User.id == int(profile.user_id),
                    User.org_id == int(profile.org_id),
                    User.status == "active",
                )
            )
        ).scalar_one_or_none()
        if user is None or not (user.user_code or "").strip():
            raise RuntimeWorkspaceNotFoundError("active platform user identity not found")
        result = await self.session.execute(
            RuntimeWorkspaceRepository.binding_by_workspace_statement(
                runtime_workspace_id=int(profile.id)
            )
        )
        binding = result.scalar_one_or_none()
        return RuntimeWorkspaceRecord(
            runtime_workspace_id=int(profile.id),
            cloud_profile_id=int(profile.id),
            org_id=int(profile.org_id),
            user_id=int(profile.user_id),
            user_name=str(user.name),
            user_code=str(user.user_code),
            name=profile.name,
            status=profile.status,
            default_model_id=int(profile.default_model_id) if profile.default_model_id else None,
            runtime_status=getattr(binding, "status", None),
            pending_reload=bool(getattr(binding, "pending_reload", False))
            if binding is not None
            else False,
            config_generation=int(getattr(binding, "config_generation", 0) or 0)
            if binding is not None
            else 0,
        )

    async def update_skill_policy(
        self, *, principal: Principal, policy_version: int, expected_config_generation: int
    ) -> SkillPolicyUpdateResult:
        if policy_version not in {
            SKILL_VISIBILITY_POLICY_V1,
            SKILL_VISIBILITY_POLICY_V2,
            SKILL_VISIBILITY_POLICY_V3,
        }:
            raise ValueError("unsupported Skill visibility policy")
        profile = await self.session.scalar(
            select(CloudHermesProfile)
            .where(
                CloudHermesProfile.org_id == principal.org_id,
                CloudHermesProfile.user_id == principal.user_id,
                CloudHermesProfile.profile_type == "default_chat_agent",
                CloudHermesProfile.status == "active",
            )
            .with_for_update()
        )
        if profile is None:
            raise RuntimeWorkspaceNotFoundError("runtime workspace not found")
        binding = await self.session.scalar(
            select(HermesRuntimeBinding)
            .where(HermesRuntimeBinding.runtime_workspace_id == int(profile.id))
            .with_for_update()
        )
        if binding is None:
            raise RuntimeWorkspaceNotFoundError("runtime binding not found")
        current = int(binding.config_generation or 0)
        if current != expected_config_generation:
            await self.session.rollback()
            raise RuntimeSkillPolicyConflictError(RuntimeSkillPolicyConflictError.code)
        config = dict(profile.profile_config or {})
        config[SKILL_POLICY_CONFIG_KEY] = policy_version
        profile.profile_config = config
        binding.config_generation = current + 1
        binding.pending_reload = True
        scopes = set(binding.pending_reload_scopes or [])
        scopes.add("skills")
        binding.pending_reload_scopes = sorted(scopes)
        await self.session.commit()
        return SkillPolicyUpdateResult(
            runtime_workspace_id=int(profile.id),
            policy_version=policy_version,
            config_generation=current + 1,
            pending_reload=True,
        )

    async def _ensure_default_agent_profile(
        self,
        profile: CloudHermesProfile,
    ) -> RuntimeAgentProfile:
        agent_profile = (
            (
                await self.session.execute(
                    select(RuntimeAgentProfile).where(
                        RuntimeAgentProfile.runtime_workspace_id == int(profile.id),
                        RuntimeAgentProfile.profile_kind == "default",
                    )
                )
            )
            .scalars()
            .first()
        )
        if agent_profile is not None:
            return agent_profile
        agent_profile = await SqlAlchemyRuntimeAgentProfileStore(self.session).ensure_default(
            workspace=profile,
        )
        await self.session.commit()
        await self.session.refresh(agent_profile)
        return agent_profile

    @staticmethod
    def _plan_from_binding(
        record: RuntimeWorkspaceRecord,
        binding: HermesRuntimeBinding,
    ) -> RuntimeBindingPlan:
        if not binding.runtime_root_pvc or not binding.runtime_root_host_path:
            raise RuntimeConfigPreflightError("runtime binding storage identity is incomplete")
        return RuntimeBindingPlan(
            org_id=record.org_id,
            user_id=record.user_id,
            user_name=record.user_name,
            user_code=record.user_code,
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_id=binding.runtime_id,
            service_name=binding.knative_service,
            secret_name=f"{binding.knative_service}-api-key",
            hermes_home_sub_path=binding.hermes_home_sub_path,
            workspace_sub_path=binding.workspace_sub_path,
            runtime_root_pvc_name=binding.runtime_root_pvc,
            runtime_root_host_path=binding.runtime_root_host_path,
            binding_id=str(getattr(binding, "id", binding.runtime_id)),
            config_revision=str(
                getattr(binding, "config_hash", None)
                or (f"generation-{int(getattr(binding, 'config_generation', 0) or 0)}")
            ),
            runtime_config_source=binding.runtime_config_source,
            image=str(getattr(binding, "image", "") or ""),
            image_digest=(str(getattr(binding, "image_digest", "") or "").strip() or None),
            runtime_root_pvc_uid=getattr(binding, "runtime_root_pvc_uid", None),
            pvc_storage_identity_hash=getattr(binding, "pvc_storage_identity_hash", None),
        )


class RuntimeWarmupRunner(Protocol):
    async def warmup(self, record: RuntimeWorkspaceRecord) -> RuntimeWorkspaceRecord: ...


class RuntimeReadyHook(Protocol):
    async def ensure_for_runtime(self, record: RuntimeWorkspaceRecord) -> None: ...


class RuntimeDirectoryProvisioner(Protocol):
    async def ensure_directory(self, spec: KnativeRuntimeSpec) -> None: ...


class RuntimeConfigBootstrapper(Protocol):
    async def ensure_config(self, spec: KnativeRuntimeSpec) -> None: ...


class LocalRuntimeDirectoryProvisioner:
    """Prepare the node-local path required by a Kubernetes local PV.

    ``local.path`` is not a hostPath volume and Kubernetes will not create it
    for us.  The platform backend is deployed on the configured runtime node,
    so this is deliberately done before PV/PVC/KService apply.
    """

    _MANAGED_DIRECTORY_MODE = 0o2770

    async def ensure_directory(self, spec: KnativeRuntimeSpec) -> None:
        from app.services.personal_config_bundle import BOOTSTRAP_CONFIG_SCHEMA

        # Bootstrap 模式的 Supervisor 以空盘合同启动（fresh probe 只允许空
        # scaffold 目录）；Backend 不得预写 USER.md/MEMORY.md 或平台记忆锁，
        # 否则全新 Runtime 会在 FRESH_STORAGE_NOT_EMPTY 上 CrashLoop。
        bootstrap_home = int(spec.config_schema) >= BOOTSTRAP_CONFIG_SCHEMA
        root = Path(spec.runtime_root_host_path)

        def create() -> None:
            try:
                root.mkdir(parents=True, exist_ok=True)
                self._require_real_directory(root, "runtime-root")
                # All three paths are mounted through volumeMount.subPath;
                # validate each component before creating the next one so a
                # historical symlink cannot redirect platform writes.
                managed = [root]
                for sub_path in (
                    spec.hermes_home_sub_path,
                    spec.workspace_sub_path,
                    spec.supervisor_data_sub_path,
                ):
                    if not sub_path:
                        continue
                    relative = Path(sub_path)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise RuntimeConfigPreflightError(
                            "runtime managed directory has unsafe relative path"
                        )
                    current = root
                    for part in relative.parts:
                        if part in {"", "."}:
                            continue
                        current = current / part
                        self._ensure_real_directory(current, sub_path)
                        if current not in managed:
                            managed.append(current)
                if spec.hermes_home_sub_path:
                    home = root / spec.hermes_home_sub_path
                    self._require_real_directory(home, "home")
                    for relative in (
                        "platform",
                        "skills",
                        "plugins",
                        "PERSONAL",
                        "memories",
                        "logs",
                    ):
                        child = home / relative
                        self._ensure_real_directory(child, relative)
                        managed.append(child)
                    if spec.application_agent_id is None and not bootstrap_home:
                        for filename in (
                            "USER.md",
                            "MEMORY.md",
                        ):
                            self._normalize_optional_managed_file(
                                home / "memories" / filename,
                                f"memories/{filename}",
                                uid=spec.runtime_user_uid,
                                gid=spec.runtime_shared_gid,
                            )
                        RuntimeUserMemoryWriter().ensure(
                            home,
                            uid=spec.runtime_user_uid,
                            gid=spec.runtime_shared_gid,
                        )
                        for relative in (
                            "kanban",
                            "kanban/workspaces",
                            "kanban/logs",
                            "kanban/boards",
                        ):
                            child = home / relative
                            self._ensure_real_directory(child, relative)
                            managed.append(child)
                        for filename in (
                            "kanban.db",
                            "kanban.db-wal",
                            "kanban.db-shm",
                        ):
                            self._normalize_optional_managed_file(
                                home / filename,
                                filename,
                                uid=spec.runtime_user_uid,
                                gid=spec.runtime_shared_gid,
                            )
                if spec.enterprise_knowledge_host_path:
                    knowledge_root = Path(spec.enterprise_knowledge_host_path)
                    required = (
                        knowledge_root / "PRIMER.md",
                        knowledge_root / "workflows" / "query.md",
                    )
                    missing = [str(path) for path in required if not path.is_file()]
                    if missing:
                        raise RuntimeConfigPreflightError(
                            "enterprise knowledge preflight failed: missing " + ", ".join(missing)
                        )
                for directory in managed:
                    self._normalize_directory(
                        directory,
                        uid=spec.runtime_user_uid,
                        gid=spec.runtime_shared_gid,
                    )
            except RuntimeConfigPreflightError:
                raise
            except OSError as exc:
                raise RuntimeConfigPreflightError(
                    "runtime managed directory preflight failed"
                ) from exc

        await asyncio.to_thread(create)

    @staticmethod
    def _require_real_directory(path: Path, label: str) -> None:
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise RuntimeConfigPreflightError(
                f"runtime managed directory is unavailable: {label}"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeConfigPreflightError(
                f"runtime managed directory is not a real directory: {label}"
            )

    @classmethod
    def _ensure_real_directory(cls, path: Path, label: str) -> None:
        try:
            path.mkdir(mode=cls._MANAGED_DIRECTORY_MODE, exist_ok=True)
        except OSError as exc:
            raise RuntimeConfigPreflightError(
                f"cannot create runtime managed directory: {label}"
            ) from exc
        cls._require_real_directory(path, label)

    @classmethod
    def _normalize_directory(cls, path: Path, *, uid: int, gid: int) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise RuntimeConfigPreflightError(
                    "runtime managed directory changed type during preflight"
                )
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, cls._MANAGED_DIRECTORY_MODE)
        finally:
            os.close(descriptor)

    @staticmethod
    def _normalize_optional_managed_file(
        path: Path,
        label: str,
        *,
        uid: int,
        gid: int,
    ) -> None:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeConfigPreflightError(
                f"runtime managed storage is unavailable: {label}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeConfigPreflightError(
                f"runtime managed storage is not a regular file: {label}"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RuntimeConfigPreflightError(
                f"cannot open runtime managed storage: {label}"
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeConfigPreflightError(
                    f"runtime managed storage changed type: {label}"
                )
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o660)
        finally:
            os.close(descriptor)


class RuntimeTopologyReconcilerProtocol(Protocol):
    async def reconcile(
        self,
        record: RuntimeWorkspaceRecord,
        binding: RuntimeBindingEnsureResult,
    ) -> RuntimeWorkspaceRecord: ...

    async def kservice_exists(self, service_name: str) -> bool: ...


class RuntimeWarmupAdmission(Protocol):
    async def require_new_work_allowed(self, *, lock: bool = False) -> object: ...


class RuntimeKServiceWarmupRunner:
    def __init__(
        self,
        *,
        binding_store: RuntimeWorkspaceStore,
        topology_reconciler: RuntimeTopologyReconcilerProtocol,
        admission_service: RuntimeWarmupAdmission,
        ready_hook: RuntimeReadyHook | None = None,
    ) -> None:
        self.binding_store = binding_store
        self.topology_reconciler = topology_reconciler
        self.admission_service = admission_service
        self.ready_hook = ready_hook

    async def warmup(self, record: RuntimeWorkspaceRecord) -> RuntimeWorkspaceRecord:
        await self.admission_service.require_new_work_allowed(lock=True)
        binding = await self.binding_store.ensure_binding(record)
        return await self.warmup_bound(record, binding)

    async def warmup_bound(
        self,
        record: RuntimeWorkspaceRecord,
        binding: RuntimeBindingEnsureResult,
    ) -> RuntimeWorkspaceRecord:
        await self.admission_service.require_new_work_allowed(lock=True)
        if (
            binding.plan.runtime_workspace_id != record.runtime_workspace_id
            or binding.plan.org_id != record.org_id
            or binding.plan.user_id != record.user_id
        ):
            raise RuntimeConfigPreflightError(
                "Runtime bootstrap binding identity does not match workspace"
            )
        if not binding.reconcile_required and not await self.topology_reconciler.kservice_exists(
            binding.plan.service_name
        ):
            binding = replace(
                binding,
                reconcile_required=True,
            )
        if not binding.reconcile_required:
            ready_record = replace(
                record,
                runtime_status="ready",
                pending_reload=False,
            )
        else:
            ready_record = await self.topology_reconciler.reconcile(record, binding)
        if ready_record.runtime_status == "ready" and self.ready_hook is not None:
            await self.ready_hook.ensure_for_runtime(ready_record)
        return ready_record


class KnativeRuntimeClient(Protocol):
    async def apply(self, manifest: dict[str, Any]) -> None: ...

    async def zero_initial_scale_allowed(self) -> bool: ...

    async def get_kservice(self, *, namespace: str, name: str) -> dict[str, Any]: ...

    async def get_persistent_volume_claim(
        self,
        *,
        namespace: str,
        name: str,
    ) -> dict[str, Any]: ...

    async def delete_kservice(self, *, namespace: str, name: str) -> None: ...

    async def list_kservice_pods(
        self,
        *,
        namespace: str,
        service_name: str,
    ) -> list[dict[str, Any]]: ...

    async def delete_service(self, *, namespace: str, name: str) -> None: ...


class RuntimeBindingStatusStore(Protocol):
    async def update_binding_status(self, patch: RuntimeBindingStatusPatch) -> None: ...

    async def bind_pvc_identity(
        self,
        *,
        runtime_workspace_id: int,
        pvc_uid: str,
        pvc_storage_identity_hash: str,
    ) -> None: ...


class SqlAlchemyRuntimeBindingStatusStore:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def update_binding_status(self, patch: RuntimeBindingStatusPatch) -> None:
        statement = RuntimeWorkspaceRepository.update_runtime_binding_status_statement(
            runtime_workspace_id=patch.runtime_workspace_id,
            values={
                "knative_status_url": patch.knative_status_url,
                "route_host": patch.route_host,
                "endpoint_base_url": patch.endpoint_base_url,
                "endpoint_host_header": patch.endpoint_host_header,
                "status": patch.status,
                "last_error": patch.last_error,
                "pending_reload": patch.status != "ready",
            },
        )
        await self.session.execute(statement)
        await self.session.commit()

    async def bind_pvc_identity(
        self,
        *,
        runtime_workspace_id: int,
        pvc_uid: str,
        pvc_storage_identity_hash: str,
    ) -> None:
        binding = (
            await self.session.execute(
                select(HermesRuntimeBinding)
                .where(HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id)
                .with_for_update()
            )
        ).scalar_one()
        try:
            bind_persisted_pvc_identity(
                binding,
                pvc_uid=pvc_uid,
                pvc_storage_identity_hash=pvc_storage_identity_hash,
            )
        except RuntimeError as exc:
            raise RuntimeConfigPreflightError(str(exc)) from exc
        await self.session.commit()


class RuntimeWorkspaceService:
    def __init__(self, store: RuntimeWorkspaceStore) -> None:
        self.store = store

    async def ensure_default_for_user(self, principal: Principal) -> RuntimeWorkspaceRecord:
        profile = await self.store.get_default_profile(
            org_id=principal.org_id,
            user_id=principal.user_id,
        )
        if profile is None:
            profile = await self.store.create_default_profile(
                org_id=principal.org_id,
                user_id=principal.user_id,
                name="默认数字人",
            )
        return profile

    async def warmup_default_for_user(
        self,
        principal: Principal,
        warmup_runner: RuntimeWarmupRunner,
    ) -> RuntimeWorkspaceRecord:
        profile = await self.ensure_default_for_user(principal)
        return await warmup_runner.warmup(profile)

    async def keepalive_default_for_user(
        self,
        principal: Principal,
    ) -> RuntimeWorkspaceRecord:
        profile = await self.store.get_default_profile(
            org_id=principal.org_id,
            user_id=principal.user_id,
        )
        if profile is None:
            raise RuntimeKeepaliveError("runtime workspace not found")
        if profile.runtime_status != "ready":
            raise RuntimeKeepaliveError("runtime is not ready")
        return profile

    async def update_skill_policy(
        self,
        *,
        principal: Principal,
        policy_version: int,
        expected_config_generation: int,
    ) -> SkillPolicyUpdateResult:
        return await self.store.update_skill_policy(
            principal=principal,
            policy_version=policy_version,
            expected_config_generation=expected_config_generation,
        )

    @staticmethod
    def build_default_binding_plan(
        record: RuntimeWorkspaceRecord,
        *,
        runtime_root_path: str | None = None,
    ) -> RuntimeBindingPlan:
        key = runtime_resource_key(record.user_code)
        runtime_id = f"rt-{record.runtime_workspace_id}"
        service_name = f"runtime-{key}"
        return RuntimeBindingPlan(
            org_id=record.org_id,
            user_id=record.user_id,
            user_name=record.user_name,
            user_code=record.user_code,
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_id=runtime_id,
            service_name=service_name,
            secret_name=f"{service_name}-api-key",
            hermes_home_sub_path="home",
            workspace_sub_path="workspace",
            runtime_root_pvc_name=f"{service_name}-root",
            runtime_root_host_path=runtime_root_host_path(
                runtime_root_path=runtime_root_path or settings.HERMES_RUNTIME_ROOT,
                org_id=record.org_id,
                resource_key=key,
            ),
            binding_id="",
            runtime_config_source="pvc",
        )


class RuntimeReadyProtocol(Protocol):
    async def health(self, endpoint: RuntimeEndpoint) -> dict[str, Any]: ...

    async def capabilities(self, endpoint: RuntimeEndpoint) -> dict[str, Any]: ...


class RuntimeBindingReadyService:
    def __init__(
        self,
        *,
        k8s_client: KnativeRuntimeClient,
        binding_store: RuntimeBindingStatusStore,
        protocol_adapter: RuntimeReadyProtocol,
        provider: KnativeRuntimeProvider | None = None,
        ready_timeout_seconds: float = 120.0,
        startup_reconciler: Callable[[KnativeRuntimeSpec], Awaitable[None]] | None = None,
        schema_startup_preparer: (
            Callable[[KnativeRuntimeSpec, str], Awaitable[KnativeRuntimeSpec]] | None
        ) = None,
        bootstrap_completion: (Callable[[KnativeRuntimeSpec], Awaitable[None]] | None) = None,
    ) -> None:
        if ready_timeout_seconds <= 0:
            raise ValueError("ready_timeout_seconds must be positive")
        self.k8s_client = k8s_client
        self.binding_store = binding_store
        self.protocol_adapter = protocol_adapter
        self.provider = provider or KnativeRuntimeProvider()
        self.ready_timeout_seconds = float(ready_timeout_seconds)
        self.startup_reconciler = startup_reconciler
        self.schema_startup_preparer = schema_startup_preparer
        self.bootstrap_completion = bootstrap_completion

    @staticmethod
    def build_knative_spec(
        *,
        plan: RuntimeBindingPlan,
        namespace: str,
        image: str,
        service_account: str,
        startup_mode: str,
        runtime_node_name: str = "demo",
        default_model_entry_id: int | None = None,
        platform_api_base_url: str | None = None,
        enterprise_knowledge_host_path: str | None = None,
    ) -> KnativeRuntimeSpec:
        return KnativeRuntimeSpec(
            org_id=plan.org_id,
            user_id=plan.user_id,
            user_name=plan.user_name,
            user_code=plan.user_code,
            runtime_workspace_id=plan.runtime_workspace_id,
            runtime_id=plan.runtime_id,
            namespace=namespace,
            service_name=plan.service_name,
            image=image,
            service_account=service_account,
            api_server_key_secret_name=plan.secret_name,
            runtime_root_pvc_name=plan.runtime_root_pvc_name,
            hermes_home_sub_path=plan.hermes_home_sub_path,
            workspace_sub_path=plan.workspace_sub_path,
            runtime_root_pv_name=f"{plan.runtime_root_pvc_name}-pv",
            runtime_root_host_path=plan.runtime_root_host_path,
            runtime_node_name=runtime_node_name,
            upstream_commit=plan.upstream_commit,
            state_schema=plan.state_schema,
            config_schema=plan.config_schema,
            startup_mode=startup_mode,
            platform_control_secret_name=(plan.platform_control_secret_name),
            platform_control_key_version=plan.platform_control_key_version,
            runtime_root_pvc_uid=plan.runtime_root_pvc_uid,
            pvc_storage_identity_hash=plan.pvc_storage_identity_hash,
            default_model_entry_id=default_model_entry_id,
            long_request_timeout_seconds=settings.HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS,
            idle_ttl_seconds=settings.HERMES_RUNTIME_IDLE_TTL_SECONDS,
            ws_orphan_reap_grace_seconds=settings.HERMES_TUI_WS_ORPHAN_REAP_GRACE_S,
            platform_api_base_url=platform_api_base_url or settings.platform_api_base_url,
            enterprise_knowledge_host_path=(
                enterprise_knowledge_host_path
                if enterprise_knowledge_host_path is not None
                else (settings.ENTERPRISE_KNOWLEDGE_HOST_PATH or None)
            ),
            runtime_binding_id=plan.binding_id or plan.runtime_id,
            runtime_config_revision=plan.config_revision,
            backend_internal_base_url=settings.runtime_internal_base_url,
            newapi_base_url=settings.NEWAPI_BASE_URL,
            newapi_api_key_secret_name=settings.NEWAPI_API_KEY_SECRET_NAME,
            post_turn_review_activation_window_seconds=(
                settings.POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS
            ),
            post_turn_review_idle_timeout_seconds=(settings.POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS),
            post_turn_review_max_duration_seconds=(settings.POST_TURN_REVIEW_MAX_DURATION_SECONDS),
            provider_route_acceptance_mode=(settings.RUNTIME_PROVIDER_ROUTE_ACCEPTANCE_MODE),
            personal_runtime_cpu_request=settings.PERSONAL_RUNTIME_CPU_REQUEST,
            personal_runtime_memory_request=settings.PERSONAL_RUNTIME_MEMORY_REQUEST,
        )

    async def ensure_kservice_ready(
        self,
        *,
        spec: KnativeRuntimeSpec,
        api_server_key: str,
        platform_control_key: RuntimePlatformControlKeyMaterial,
        ingress_base_url: str,
        zero_initial_scale: bool = False,
    ) -> RuntimeBindingStatusPatch:
        status_payload: dict[str, Any] = {
            "knative_status_url": None,
            "route_host": None,
            "endpoint_base_url": None,
            "endpoint_host_header": None,
            "status": "failed",
            "last_error": None,
        }
        try:
            if zero_initial_scale and not await self.k8s_client.zero_initial_scale_allowed():
                raise RuntimeConfigPreflightError(
                    "Knative does not allow zero initial scale"
                )
            await self.k8s_client.apply(
                self.provider.build_local_pv_manifest(
                    name=spec.runtime_root_pv_name,
                    host_path=spec.runtime_root_host_path,
                    node_name=spec.runtime_node_name,
                    storage_size=spec.pvc_storage_size,
                    access_mode=spec.runtime_root_access_mode,
                )
            )
            await self.k8s_client.apply(
                self.provider.build_static_pvc_manifest(
                    namespace=spec.namespace,
                    name=spec.runtime_root_pvc_name,
                    volume_name=spec.runtime_root_pv_name,
                    storage_size=spec.pvc_storage_size,
                    access_mode=spec.runtime_root_access_mode,
                )
            )
            pvc = await self.k8s_client.get_persistent_volume_claim(
                namespace=spec.namespace,
                name=spec.runtime_root_pvc_name,
            )
            pvc_uid = str((pvc.get("metadata") or {}).get("uid") or "").strip()
            if not pvc_uid:
                raise RuntimeConfigPreflightError("Runtime PVC immutable UID is unavailable")
            pvc_identity_hash = runtime_pvc_storage_identity_hash(
                namespace=spec.namespace,
                pvc_uid=pvc_uid,
            )
            if (spec.runtime_root_pvc_uid is None) != (spec.pvc_storage_identity_hash is None):
                raise RuntimeConfigPreflightError("Runtime binding PVC identity is incomplete")
            if spec.runtime_root_pvc_uid is not None and (
                spec.runtime_root_pvc_uid != pvc_uid
                or spec.pvc_storage_identity_hash != pvc_identity_hash
            ):
                raise RuntimeConfigPreflightError("Runtime binding PVC identity changed")
            await self.binding_store.bind_pvc_identity(
                runtime_workspace_id=spec.runtime_workspace_id,
                pvc_uid=pvc_uid,
                pvc_storage_identity_hash=pvc_identity_hash,
            )
            spec = replace(
                spec,
                runtime_root_pvc_uid=pvc_uid,
                pvc_storage_identity_hash=pvc_identity_hash,
            )
            await self.k8s_client.apply(
                self.provider.build_platform_control_secret_manifest(
                    spec=spec,
                    material=platform_control_key,
                )
            )
            for manifest in self.provider.build_enterprise_knowledge_volume_manifests(spec):
                await self.k8s_client.apply(manifest)
            await self.k8s_client.apply(
                self.provider.build_secret_manifest(
                    namespace=spec.namespace,
                    name=spec.api_server_key_secret_name,
                    api_server_key=api_server_key,
                )
            )
            if self.schema_startup_preparer is not None:
                spec = await self.schema_startup_preparer(spec, ingress_base_url)
            if zero_initial_scale and spec.startup_mode != "steady_state":
                raise RuntimeConfigPreflightError(
                    "zero-scale Runtime precreation only supports steady_state"
                )
            manifest_spec = (
                replace(spec, zero_initial_scale=True)
                if zero_initial_scale
                else spec
            )
            await self.k8s_client.apply(
                self.provider.build_kservice_manifest(manifest_spec)
            )

            kservice = await self._wait_for_kservice_ready(
                namespace=spec.namespace,
                name=spec.service_name,
                timeout_seconds=self.ready_timeout_seconds,
            )
            status_payload = self.provider.parse_status(
                kservice,
                endpoint_base_url=ingress_base_url,
            )
            if status_payload.get("status") != "ready":
                raise RuntimeError(
                    status_payload.get("last_error")
                    or "Hermes Runtime KService did not become ready"
                )

            starting_patch = self._patch_from_status(
                spec.runtime_workspace_id,
                status_payload,
                status="starting",
                last_error=None,
            )
            await self.binding_store.update_binding_status(starting_patch)

            if not starting_patch.endpoint_base_url or not starting_patch.endpoint_host_header:
                raise RuntimeError("runtime Dashboard endpoint is missing")
            if zero_initial_scale:
                ready_patch = self._patch_from_status(
                    spec.runtime_workspace_id,
                    status_payload,
                    status="ready",
                    last_error=None,
                )
                await self.binding_store.update_binding_status(ready_patch)
                await self.k8s_client.delete_service(
                    namespace=spec.namespace,
                    name=f"{spec.service_name}-api",
                )
                return ready_patch
            endpoint = RuntimeEndpoint(
                runtime_workspace_id=spec.runtime_workspace_id,
                runtime_id=spec.runtime_id,
                endpoint_base_url=starting_patch.endpoint_base_url,
                endpoint_host_header=starting_patch.endpoint_host_header,
                api_key=api_server_key,
            )
            if self.startup_reconciler is not None:
                await self.startup_reconciler(spec)
            await self.protocol_adapter.health(endpoint)
            if spec.startup_mode == "fresh_bootstrap":
                if self.bootstrap_completion is None:
                    raise RuntimeError("fresh bootstrap completion is unavailable")
                await self.bootstrap_completion(spec)
                await self._hard_replace_fresh_bootstrap_kservice(spec)
                spec = replace(
                    spec,
                    startup_mode="steady_state",
                    bootstrap_intent_id=None,
                    bootstrap_authorization_digest=None,
                )
                await self.k8s_client.apply(self.provider.build_kservice_manifest(spec))
                steady_kservice = await self._wait_for_kservice_ready(
                    namespace=spec.namespace,
                    name=spec.service_name,
                    timeout_seconds=self.ready_timeout_seconds,
                )
                steady_status = self.provider.parse_status(
                    steady_kservice,
                    endpoint_base_url=ingress_base_url,
                )
                if steady_status.get("status") != "ready":
                    raise RuntimeError(
                        steady_status.get("last_error")
                        or "steady-state Runtime KService did not become ready"
                    )
                await self.protocol_adapter.health(endpoint)
            await self.k8s_client.delete_service(
                namespace=spec.namespace,
                name=f"{spec.service_name}-api",
            )
        except Exception as exc:
            failed_patch = self._patch_from_status(
                spec.runtime_workspace_id,
                status_payload,
                status="failed",
                last_error=f"runtime reconcile failed: {exc}",
            )
            await self.binding_store.update_binding_status(failed_patch)
            return failed_patch

        ready_patch = self._patch_from_status(
            spec.runtime_workspace_id,
            status_payload,
            status="ready",
            last_error=None,
        )
        await self.binding_store.update_binding_status(ready_patch)
        return ready_patch

    async def activate_matching_kservice(
        self,
        *,
        spec: KnativeRuntimeSpec,
        api_server_key: str,
        ingress_base_url: str,
    ) -> RuntimeBindingStatusPatch | None:
        """Activate an exactly matching precreated zero-scale KService.

        ``None`` means the live KService does not own the target contract and the
        caller must use the ordinary hard-replacement path.
        """

        kservice = await self.k8s_client.get_kservice(
            namespace=spec.namespace,
            name=spec.service_name,
        )
        if not self.provider.precreated_kservice_matches(kservice, spec):
            return None
        if await self.k8s_client.list_kservice_pods(
            namespace=spec.namespace,
            service_name=spec.service_name,
        ):
            return None
        status_payload = self.provider.parse_status(
            kservice,
            endpoint_base_url=ingress_base_url,
        )
        starting_patch = self._patch_from_status(
            spec.runtime_workspace_id,
            status_payload,
            status="starting",
            last_error=None,
        )
        await self.binding_store.update_binding_status(starting_patch)
        endpoint = RuntimeEndpoint(
            runtime_workspace_id=spec.runtime_workspace_id,
            runtime_id=spec.runtime_id,
            endpoint_base_url=starting_patch.endpoint_base_url,
            endpoint_host_header=starting_patch.endpoint_host_header,
            api_key=api_server_key,
        )
        await self.protocol_adapter.health(endpoint)
        ready_patch = self._patch_from_status(
            spec.runtime_workspace_id,
            status_payload,
            status="ready",
            last_error=None,
        )
        await self.binding_store.update_binding_status(ready_patch)
        return ready_patch

    async def has_matching_zero_scale_kservice(
        self,
        *,
        spec: KnativeRuntimeSpec,
    ) -> bool:
        """Read-only probe for a zero-scale target without rendering config."""

        try:
            kservice = await self.k8s_client.get_kservice(
                namespace=spec.namespace,
                name=spec.service_name,
            )
        except KubectlCommandError as exc:
            if f'"{spec.service_name}" not found' not in str(exc).lower():
                raise
            return False
        if not self.provider.precreated_kservice_matches(
            kservice,
            spec,
            require_initial_config=False,
        ):
            return False
        return not await self.k8s_client.list_kservice_pods(
            namespace=spec.namespace,
            service_name=spec.service_name,
        )

    async def _hard_replace_fresh_bootstrap_kservice(
        self,
        spec: KnativeRuntimeSpec,
    ) -> None:
        """Prevent fresh-bootstrap and steady-state Pods sharing one PVC."""
        await self.k8s_client.delete_kservice(
            namespace=spec.namespace,
            name=spec.service_name,
        )
        deadline = asyncio.get_running_loop().time() + self.ready_timeout_seconds
        while await self.k8s_client.list_kservice_pods(
            namespace=spec.namespace,
            service_name=spec.service_name,
        ):
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("fresh bootstrap Pods did not terminate")
            await asyncio.sleep(1)

    async def _wait_for_kservice_ready(
        self,
        *,
        namespace: str,
        name: str,
        timeout_seconds: float,
        poll_interval_seconds: float = 2.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            try:
                kservice = await self.k8s_client.get_kservice(
                    namespace=namespace,
                    name=name,
                )
            except RuntimeConfigPreflightError as exc:
                if (
                    "(NotFound)" not in str(exc)
                    or asyncio.get_running_loop().time() >= deadline
                ):
                    raise
                await asyncio.sleep(poll_interval_seconds)
                continue
            status = kservice.get("status") or {}
            ready_condition = self._condition(status, "Ready")
            if ready_condition.get("status") == "True":
                return kservice
            if ready_condition.get("status") == "False":
                return kservice
            if asyncio.get_running_loop().time() >= deadline:
                return kservice
            await asyncio.sleep(poll_interval_seconds)

    @staticmethod
    def _condition(status: dict[str, Any], condition_type: str) -> dict[str, Any]:
        conditions = status.get("conditions") or []
        return next((item for item in conditions if item.get("type") == condition_type), {})

    @staticmethod
    def _patch_from_status(
        runtime_workspace_id: int,
        status_payload: dict[str, Any],
        *,
        status: str | None = None,
        last_error: str | None = None,
    ) -> RuntimeBindingStatusPatch:
        return RuntimeBindingStatusPatch(
            runtime_workspace_id=runtime_workspace_id,
            knative_status_url=status_payload.get("knative_status_url"),
            route_host=status_payload.get("route_host"),
            endpoint_base_url=status_payload.get("endpoint_base_url"),
            endpoint_host_header=status_payload.get("endpoint_host_header"),
            status=status or status_payload.get("status") or "failed",
            last_error=(last_error if status is not None else status_payload.get("last_error")),
        )
