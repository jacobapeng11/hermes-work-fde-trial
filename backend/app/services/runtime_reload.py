from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationAgentRuntimeBinding,
    HermesRuntimeBinding,
    ProfileConvergenceJob,
    RuntimeAgentProfile,
    RuntimeMcpRegistryJob,
    RuntimeMcpRegistryState,
)
from app.services.application_agent_runtime import SqlAlchemyApplicationAgentRuntimeManager
from app.services.runtime_image_config import RuntimeImageConfig
from app.services.runtime_infrastructure import build_runtime_infrastructure
from app.services.runtime_activity import SqlAlchemyRuntimeActivityStore
from app.services.runtime_maintenance import RuntimeMaintenanceService


RuntimeBindingType = Literal["personal_runtime", "application_runtime"]


class RuntimeStatus(str, Enum):
    ABSENT = "absent"
    SCALED_TO_ZERO = "scaled_to_zero"
    READY_IDLE = "ready_idle"
    ACTIVE = "active"


class ProjectionScope(str, Enum):
    PERSONA = "persona"
    SKILLS = "skills"
    MCP = "mcp"
    KNOWLEDGE = "knowledge"
    EXPERTS = "experts"
    MODEL = "model"
    MODEL_POLICY = "model_policy"
    SECURITY = "security"
    RUNTIME_IMAGE = "runtime_image"


class ReloadAction(str, Enum):
    UPDATE_TEMPLATE = "update_template"
    HOT_RELOAD = "hot_reload"
    RESTART_POD = "restart_pod"
    MARK_PENDING_RELOAD = "mark_pending_reload"
    REJECT_NEW_RUN_UNTIL_REFRESHED = "reject_new_run_until_refreshed"


@dataclass(frozen=True)
class ReloadDecisionInput:
    runtime_status: RuntimeStatus
    scopes: set[ProjectionScope]
    active_run_count: int = 0
    hot_reload_supported: bool = False


@dataclass(frozen=True)
class ReloadDecision:
    action: ReloadAction
    pending_reload: bool
    allow_new_run: bool
    reason: str


class RuntimeReloadDecisionService:
    """Normal configuration reload policy; separate from hard image replacement."""

    STRICT_SCOPES = {ProjectionScope.SECURITY, ProjectionScope.MODEL_POLICY}
    RESTART_SCOPES = {
        ProjectionScope.MODEL,
        ProjectionScope.SECURITY,
        ProjectionScope.MODEL_POLICY,
        ProjectionScope.RUNTIME_IMAGE,
    }

    def decide(self, payload: ReloadDecisionInput) -> ReloadDecision:
        if not payload.scopes:
            return ReloadDecision(
                ReloadAction.HOT_RELOAD, False, True, "no projection scopes changed"
            )
        if payload.runtime_status in {RuntimeStatus.ABSENT, RuntimeStatus.SCALED_TO_ZERO}:
            return ReloadDecision(
                ReloadAction.UPDATE_TEMPLATE,
                False,
                True,
                "runtime is not serving requests; next activation reads latest projection",
            )
        if payload.runtime_status == RuntimeStatus.ACTIVE or payload.active_run_count > 0:
            if self.STRICT_SCOPES.intersection(payload.scopes):
                return ReloadDecision(
                    ReloadAction.REJECT_NEW_RUN_UNTIL_REFRESHED,
                    True,
                    False,
                    "security or model policy change must apply before any new run",
                )
            return ReloadDecision(
                ReloadAction.MARK_PENDING_RELOAD,
                True,
                True,
                "runtime has active work; apply projection after current run completes",
            )
        if payload.runtime_status == RuntimeStatus.READY_IDLE:
            if self.RESTART_SCOPES.intersection(payload.scopes):
                return ReloadDecision(
                    ReloadAction.RESTART_POD,
                    False,
                    True,
                    "changed scope requires fresh process before next run",
                )
            if payload.hot_reload_supported:
                return ReloadDecision(
                    ReloadAction.HOT_RELOAD,
                    False,
                    True,
                    "runtime is idle and supports hot reload for changed scopes",
                )
            return ReloadDecision(
                ReloadAction.RESTART_POD,
                False,
                True,
                "runtime is idle but hot reload is not verified",
            )
        return ReloadDecision(
            ReloadAction.REJECT_NEW_RUN_UNTIL_REFRESHED,
            True,
            False,
            "runtime status is not safe for reload decision",
        )


class RuntimeReloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeReloadBinding:
    binding_type: RuntimeBindingType
    binding_id: int
    runtime_id: str
    namespace: str
    service_name: str
    pvc_name: str
    pvc_uid: str
    pvc_storage_identity_hash: str
    image: str
    image_digest: str | None
    status: str


class RuntimeReloadService:
    """Backend-owned complete projection for code-only Runtime replacement."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve_binding(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
    ) -> RuntimeReloadBinding:
        model = (
            HermesRuntimeBinding
            if binding_type == "personal_runtime"
            else ApplicationAgentRuntimeBinding
        )
        row = await self.db.scalar(select(model).where(model.id == int(binding_id)))
        if row is None:
            raise RuntimeReloadError("Runtime Binding does not exist")
        pvc_name = (
            str(row.runtime_root_pvc)
            if binding_type == "personal_runtime"
            else str(row.runtime_root_pvc)
        )
        if not row.runtime_root_pvc_uid or not row.pvc_storage_identity_hash:
            raise RuntimeReloadError("Runtime Binding PVC identity is incomplete")
        return RuntimeReloadBinding(
            binding_type=binding_type,
            binding_id=int(row.id),
            runtime_id=str(row.runtime_id),
            namespace=str(row.k8s_namespace),
            service_name=str(row.knative_service),
            pvc_name=pvc_name,
            pvc_uid=str(row.runtime_root_pvc_uid),
            pvc_storage_identity_hash=str(row.pvc_storage_identity_hash),
            image=str(row.image),
            image_digest=str(row.image_digest) if row.image_digest else None,
            status=str(row.status),
        )

    async def list_bindings(
        self,
        *,
        runtime_id: str | None = None,
        binding_type: RuntimeBindingType | None = None,
    ) -> list[RuntimeReloadBinding]:
        selected_types: tuple[RuntimeBindingType, ...] = (
            (binding_type,)
            if binding_type is not None
            else ("personal_runtime", "application_runtime")
        )
        result: list[RuntimeReloadBinding] = []
        for selected in selected_types:
            model = (
                HermesRuntimeBinding
                if selected == "personal_runtime"
                else ApplicationAgentRuntimeBinding
            )
            statement = select(model).where(
                model.runtime_root_pvc_uid.is_not(None),
                model.pvc_storage_identity_hash.is_not(None),
            )
            if runtime_id is not None:
                statement = statement.where(model.runtime_id == runtime_id)
            rows = list(await self.db.scalars(statement.order_by(model.id)))
            for row in rows:
                result.append(
                    await self.resolve_binding(
                        binding_type=selected,
                        binding_id=int(row.id),
                    )
                )
        return result

    async def reload_runtime(
        self,
        binding: RuntimeReloadBinding,
        *,
        target: RuntimeImageConfig,
        maintenance_epoch: str | None = None,
    ) -> RuntimeReloadBinding:
        current = await self.resolve_binding(
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
        )
        if (
            current.runtime_id != binding.runtime_id
            or current.pvc_uid != binding.pvc_uid
            or current.pvc_storage_identity_hash != binding.pvc_storage_identity_hash
        ):
            raise RuntimeReloadError("Runtime Binding identity changed before reload")
        if binding.binding_type == "personal_runtime":
            infrastructure = build_runtime_infrastructure(self.db)
            record, owned = await infrastructure.workspace_store.prepare_reload(
                runtime_id=binding.runtime_id,
                target=target,
                maintenance_epoch=maintenance_epoch,
            )
            await infrastructure.topology_reconciler.reload_runtime(record, owned)
            await self._requeue_personal_projections(binding_id=binding.binding_id)
            await self.db.commit()
        else:
            await SqlAlchemyApplicationAgentRuntimeManager(self.db).reload_runtime(
                binding_id=binding.binding_id,
                target=target,
                maintenance_epoch=maintenance_epoch,
            )
        completed = await self.resolve_binding(
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
        )
        if (
            completed.status != "ready"
            or completed.image != target.image
            or completed.image_digest != target.config_id
        ):
            raise RuntimeReloadError("Runtime reload did not persist the target identity")
        return completed

    async def requeue_personal_projections(self, *, binding_id: int) -> None:
        await self._requeue_personal_projections(binding_id=binding_id)

    async def _requeue_personal_projections(self, *, binding_id: int) -> None:
        """Reapply durable assets after bootstrap rewrites the base config."""

        binding = await self.db.scalar(
            select(HermesRuntimeBinding)
            .where(HermesRuntimeBinding.id == binding_id)
            .with_for_update()
        )
        if binding is None:
            raise RuntimeReloadError("Runtime Binding disappeared after reload")
        now = datetime.utcnow()
        profiles = list(
            await self.db.scalars(
                select(RuntimeAgentProfile).where(
                    RuntimeAgentProfile.runtime_workspace_id
                    == binding.runtime_workspace_id,
                    RuntimeAgentProfile.status == "active",
                    RuntimeAgentProfile.desired_revision > 0,
                )
            )
        )
        for profile in profiles:
            job = await self.db.scalar(
                select(ProfileConvergenceJob)
                .where(ProfileConvergenceJob.runtime_agent_profile_id == profile.id)
                .with_for_update()
            )
            if job is None:
                job = ProfileConvergenceJob(
                    org_id=int(profile.org_id),
                    runtime_workspace_id=int(profile.runtime_workspace_id),
                    runtime_agent_profile_id=int(profile.id),
                    target_revision=int(profile.desired_revision),
                    available_at=now,
                )
                self.db.add(job)
            job.target_revision = int(profile.desired_revision)
            job.status = "waiting"
            job.stage = "resolve_desired"
            job.available_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = None
            job.last_error_message = None

        registry = await self.db.scalar(
            select(RuntimeMcpRegistryState)
            .where(RuntimeMcpRegistryState.runtime_binding_id == binding.id)
            .with_for_update()
        )
        if registry is None or int(registry.desired_revision or 0) <= 0:
            return
        registry.status = "installing"
        registry.last_error_code = None
        registry.last_error_message = None
        job = await self.db.scalar(
            select(RuntimeMcpRegistryJob)
            .where(RuntimeMcpRegistryJob.runtime_mcp_registry_state_id == registry.id)
            .with_for_update()
        )
        if job is None:
            job = RuntimeMcpRegistryJob(
                runtime_mcp_registry_state_id=int(registry.id),
                target_revision=int(registry.desired_revision),
                available_at=now,
            )
            self.db.add(job)
        job.target_revision = int(registry.desired_revision)
        job.status = "waiting"
        job.stage = "resolve_registry"
        job.available_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        job.last_error_code = None
        job.last_error_message = None

    async def isolate_binding(
        self,
        binding: RuntimeReloadBinding,
        *,
        maintenance_epoch: str | None = None,
    ) -> RuntimeReloadBinding:
        maintenance = await RuntimeMaintenanceService(self.db).snapshot(lock=True)
        if maintenance_epoch is None:
            if maintenance.maintenance_active:
                raise RuntimeReloadError("global Runtime maintenance is frozen")
        elif (
            not maintenance.maintenance_active or maintenance.maintenance_epoch != maintenance_epoch
        ):
            raise RuntimeReloadError("Runtime maintenance ownership does not match")
        model = (
            HermesRuntimeBinding
            if binding.binding_type == "personal_runtime"
            else ApplicationAgentRuntimeBinding
        )
        row = await self.db.scalar(
            select(model).where(model.id == binding.binding_id).with_for_update()
        )
        if row is None or str(row.runtime_id) != binding.runtime_id:
            raise RuntimeReloadError("Runtime Binding identity changed before isolation")
        if (
            str(row.runtime_root_pvc_uid or "") != binding.pvc_uid
            or str(row.pvc_storage_identity_hash or "") != binding.pvc_storage_identity_hash
        ):
            raise RuntimeReloadError("Runtime PVC identity changed before isolation")
        row.status = "updating"
        if binding.binding_type == "personal_runtime":
            row.pending_reload = True
            row.last_error = None
        else:
            row.last_error_code = None
        await self.db.commit()
        return await self.resolve_binding(
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
        )

    async def switch_fleet_target(
        self,
        bindings: list[RuntimeReloadBinding],
        *,
        target: RuntimeImageConfig,
        maintenance_epoch: str,
    ) -> None:
        if not target.config_id or not target.manifest_digest:
            raise RuntimeReloadError("Runtime reload target identity is incomplete")
        maintenance = await RuntimeMaintenanceService(self.db).snapshot(lock=True)
        if not maintenance.maintenance_active or maintenance.maintenance_epoch != maintenance_epoch:
            raise RuntimeReloadError("Runtime maintenance ownership does not match")
        for binding in bindings:
            model = (
                HermesRuntimeBinding
                if binding.binding_type == "personal_runtime"
                else ApplicationAgentRuntimeBinding
            )
            row = await self.db.scalar(
                select(model).where(model.id == binding.binding_id).with_for_update()
            )
            if (
                row is None
                or str(row.runtime_id) != binding.runtime_id
                or str(row.runtime_root_pvc_uid or "") != binding.pvc_uid
                or str(row.pvc_storage_identity_hash or "") != binding.pvc_storage_identity_hash
            ):
                raise RuntimeReloadError("fleet Runtime identity changed before target switch")
            row.image = target.image
            row.image_digest = target.config_id
            row.status = "updating"
            if binding.binding_type == "personal_runtime":
                row.pending_reload = True
                row.last_error = None
            else:
                row.last_error_code = None
        await self.db.commit()

    async def defer_runtime_reload(
        self,
        binding: RuntimeReloadBinding,
        *,
        target: RuntimeImageConfig,
        maintenance_epoch: str,
    ) -> RuntimeReloadBinding:
        """Switch an idle personal Runtime without activating its Pod.

        The Binding stays admission-ready and keeps its deterministic route
        identity, while ``pending_reload`` forces the next login warmup to
        recreate the missing KService from the new target image.
        """
        if binding.binding_type != "personal_runtime":
            raise RuntimeReloadError("deferred Runtime reload only supports personal_runtime")
        if not target.config_id or not target.manifest_digest:
            raise RuntimeReloadError("Runtime reload target identity is incomplete")
        maintenance = await RuntimeMaintenanceService(self.db).snapshot(lock=True)
        if not maintenance.maintenance_active or maintenance.maintenance_epoch != maintenance_epoch:
            raise RuntimeReloadError("Runtime maintenance ownership does not match")
        row = await self.db.scalar(
            select(HermesRuntimeBinding)
            .where(HermesRuntimeBinding.id == binding.binding_id)
            .with_for_update()
        )
        if (
            row is None
            or str(row.runtime_id) != binding.runtime_id
            or str(row.runtime_root_pvc_uid or "") != binding.pvc_uid
            or str(row.pvc_storage_identity_hash or "") != binding.pvc_storage_identity_hash
        ):
            raise RuntimeReloadError("Runtime Binding identity changed before deferred reload")
        row.image = target.image
        row.image_digest = target.config_id
        row.status = "ready"
        row.pending_reload = True
        row.last_error = None
        await self.db.commit()
        await self._requeue_personal_projections(binding_id=binding.binding_id)
        await self.db.commit()
        return await self.resolve_binding(
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
        )

    async def precreate_deferred_runtime(
        self,
        binding: RuntimeReloadBinding,
        *,
        target: RuntimeImageConfig,
        maintenance_epoch: str | None = None,
    ) -> RuntimeReloadBinding:
        """Precreate a Ready zero-scale KService without activating Runtime."""

        if binding.binding_type != "personal_runtime":
            raise RuntimeReloadError("zero-scale precreation only supports personal_runtime")
        if not target.config_id or not target.manifest_digest:
            raise RuntimeReloadError("Runtime reload target identity is incomplete")
        current = await self.resolve_binding(
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
        )
        if (
            current.runtime_id != binding.runtime_id
            or current.pvc_uid != binding.pvc_uid
            or current.pvc_storage_identity_hash != binding.pvc_storage_identity_hash
        ):
            raise RuntimeReloadError("Runtime Binding identity changed before precreation")
        row = await self.db.scalar(
            select(HermesRuntimeBinding)
            .where(HermesRuntimeBinding.id == binding.binding_id)
        )
        if row is None:
            raise RuntimeReloadError("Runtime Binding disappeared before precreation")
        activity = await SqlAlchemyRuntimeActivityStore(self.db).get_snapshot(
            int(row.runtime_workspace_id)
        )
        if not activity.idle:
            raise RuntimeReloadError("zero-scale precreation requires an idle Runtime")
        infrastructure = build_runtime_infrastructure(self.db)
        record, owned = await infrastructure.workspace_store.prepare_reload(
            runtime_id=binding.runtime_id,
            target=target,
            maintenance_epoch=maintenance_epoch,
        )
        await infrastructure.topology_reconciler.precreate_zero_scale_runtime(record, owned)
        row = await self.db.scalar(
            select(HermesRuntimeBinding)
            .where(HermesRuntimeBinding.id == binding.binding_id)
            .with_for_update()
        )
        if (
            row is None
            or str(row.runtime_id) != binding.runtime_id
            or str(row.runtime_root_pvc_uid or "") != binding.pvc_uid
            or str(row.pvc_storage_identity_hash or "") != binding.pvc_storage_identity_hash
        ):
            raise RuntimeReloadError("Runtime Binding identity changed after precreation")
        row.image = target.image
        row.image_digest = target.config_id
        row.status = "ready"
        row.pending_reload = True
        row.last_error = None
        await self.db.commit()
        return await self.resolve_binding(
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
        )

    async def mark_failed(
        self,
        binding: RuntimeReloadBinding,
        *,
        error_summary: str = "runtime code reload failed",
    ) -> RuntimeReloadBinding:
        """Persist a terminal per-Binding reload failure without exposing command output."""
        model = (
            HermesRuntimeBinding
            if binding.binding_type == "personal_runtime"
            else ApplicationAgentRuntimeBinding
        )
        row = await self.db.scalar(
            select(model).where(model.id == binding.binding_id).with_for_update()
        )
        if (
            row is None
            or str(row.runtime_id) != binding.runtime_id
            or str(row.runtime_root_pvc_uid or "") != binding.pvc_uid
            or str(row.pvc_storage_identity_hash or "")
            != binding.pvc_storage_identity_hash
        ):
            raise RuntimeReloadError("Runtime Binding identity changed before failure mark")
        row.status = "failed"
        if binding.binding_type == "personal_runtime":
            row.pending_reload = True
            row.last_error = error_summary[:1000]
        else:
            row.last_error_code = "runtime_code_reload_failed"
        await self.db.commit()
        return await self.resolve_binding(
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
        )


__all__ = [
    "ProjectionScope",
    "ReloadAction",
    "ReloadDecision",
    "ReloadDecisionInput",
    "RuntimeReloadBinding",
    "RuntimeReloadDecisionService",
    "RuntimeReloadError",
    "RuntimeReloadService",
    "RuntimeStatus",
]
