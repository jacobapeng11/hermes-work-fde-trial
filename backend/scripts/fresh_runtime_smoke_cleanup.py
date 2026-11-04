"""Fail-closed cleanup for the one user created by fresh Runtime interface smoke.

This is intentionally an internal helper, not a user-deletion API.  Its only
caller holds the random SSO work number in memory and may delete only the
single personal Runtime that was created for that exact identity.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.hermes_runtime.providers.knative import KubectlKnativeClient
from app.models import (
    Base,
    AssetRolloutTarget,
    ChatFileArtifact,
    ChatLog,
    CloudHermesProfile,
    ExpertKanbanCollaboration,
    ExpertKanbanCollaborationTask,
    ExpertPackage,
    ExpertPackageVersion,
    ExpertProfileOperation,
    ExpertVersionArtifact,
    ExpertVersionKnowledgeSnapshot,
    HermesRuntimeBinding,
    InstalledExpertBinding,
    ModelUsageLog,
    PersonalConfigBundleArtifact,
    ProfileConvergenceAttempt,
    ProfileConvergenceJob,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAdmissionAudit,
    RuntimeAgentProfile,
    RuntimeAuditLog,
    RuntimeBootstrapIntent,
    RuntimeClarifyRequest,
    RuntimeKanbanLease,
    RuntimeKanbanLeaseEvent,
    RuntimeAgentProfileRevision,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryJob,
    RuntimeMcpRegistryRevision,
    RuntimeMcpRegistryState,
    RuntimePlatformControlKey,
    RuntimeProjectionReceipt,
    RuntimeRun,
    RuntimeRunEvent,
    RuntimeSession,
    ScheduledTask,
    ScheduledTaskExecutionLease,
    TemplateReview,
    TemplateVersion,
    User,
    UserRole,
    UserSkillHubInstallation,
)
from app.services.runtime_platform_control import (
    platform_control_secret_name,
    runtime_pvc_storage_identity_hash,
)
from app.services.runtime_workspace import runtime_resource_key, runtime_root_host_path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
CHECKPOINT_ROOT = REPOSITORY_ROOT / ".runtime/fresh-runtime-interface-smoke"
_WORK_NO_RE = re.compile(r"^smoke-[0-9]{10}-[0-9a-f]{12}$")
_PERSONAL_BINDING_TYPE = "personal_runtime"


class FreshRuntimeSmokeCleanupError(RuntimeError):
    """A cleanup refusal that is safe to show to an operator."""


@dataclass(frozen=True)
class FreshRuntimeSmokeAccount:
    acceptance_id: str
    org_id: int
    user_id: int
    work_no: str


@dataclass(frozen=True)
class FreshRuntimeCleanupPlan:
    account: FreshRuntimeSmokeAccount
    profile_id: int | None
    binding_id: int | None
    runtime_id: str | None
    namespace: str | None
    kservice_name: str | None
    pvc_name: str | None
    pvc_uid: str | None
    pvc_storage_identity_hash: str | None
    runtime_root: Path | None
    runtime_agent_profile_ids: tuple[int, ...]

    @property
    def pv_name(self) -> str | None:
        return f"{self.pvc_name}-pv" if self.pvc_name else None


def new_acceptance_id() -> str:
    """Return the non-secret suffix linking an in-memory smoke identity."""
    from uuid import uuid4

    return uuid4().hex[:12]


def acceptance_work_no(*, acceptance_id: str, now: datetime | None = None) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", acceptance_id):
        raise FreshRuntimeSmokeCleanupError("fresh smoke acceptance identity is invalid")
    current = now or datetime.now(timezone.utc)
    return f"smoke-{current.strftime('%Y%m%d%H')}-{acceptance_id}"


def checkpoint_path(account: FreshRuntimeSmokeAccount) -> Path:
    return CHECKPOINT_ROOT / f"{account.acceptance_id}.json"


def write_checkpoint(
    account: FreshRuntimeSmokeAccount,
    *,
    status: str,
    plan: FreshRuntimeCleanupPlan | None = None,
    counts: dict[str, int] | None = None,
    error_type: str | None = None,
) -> None:
    """Persist only identifiers and counts; never the random work number."""
    destination = checkpoint_path(account)
    previous: dict[str, object] = {}
    if destination.is_file():
        try:
            candidate = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, dict):
            previous = candidate
    payload: dict[str, object] = {
        "acceptance_id": account.acceptance_id,
        "org_id": account.org_id,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": account.user_id,
    }
    if plan is not None:
        payload.update(
            {
                "binding_id": plan.binding_id,
                "kservice_name": plan.kservice_name,
                "pvc_name": plan.pvc_name,
                "pv_name": plan.pv_name,
                "runtime_id": plan.runtime_id,
                "runtime_workspace_id": plan.profile_id,
            }
        )
    if counts:
        payload["deleted_counts"] = dict(sorted(counts.items()))
    if error_type:
        payload["error_type"] = error_type
    if status.startswith("acceptance_"):
        payload["acceptance_status"] = status.removeprefix("acceptance_")
        if error_type:
            payload["acceptance_error_type"] = error_type
    else:
        for key in ("acceptance_status", "acceptance_error_type"):
            if key in previous:
                payload[key] = previous[key]
    CHECKPOINT_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(destination)


async def cleanup_fresh_runtime(
    account: FreshRuntimeSmokeAccount,
    *,
    timeout_seconds: float,
    kubeconfig: str | None = None,
) -> dict[str, object]:
    """Remove the exact fresh-smoke closure or fail before DB deletion."""
    if timeout_seconds <= 0:
        raise FreshRuntimeSmokeCleanupError("cleanup timeout must be positive")
    plan: FreshRuntimeCleanupPlan | None = None
    try:
        plan = await discover_cleanup_plan(account)
        write_checkpoint(account, status="cleanup_started", plan=plan)
        await _cleanup_kubernetes(plan, timeout_seconds=timeout_seconds, kubeconfig=kubeconfig)
        _remove_runtime_root(plan)
        counts = await _cleanup_database(plan)
        await _verify_database_absent(plan)
    except Exception as exc:
        write_checkpoint(
            account,
            status="cleanup_failed",
            plan=plan,
            error_type=type(exc).__name__,
        )
        if isinstance(exc, FreshRuntimeSmokeCleanupError):
            raise
        raise FreshRuntimeSmokeCleanupError("fresh Runtime smoke cleanup failed") from exc
    write_checkpoint(account, status="cleanup_completed", plan=plan, counts=counts)
    return {
        "cleanup_completed": True,
        "deleted_record_types": len(counts),
        "deleted_records": sum(counts.values()),
    }


async def discover_cleanup_plan(
    account: FreshRuntimeSmokeAccount,
) -> FreshRuntimeCleanupPlan:
    """Read and validate the only acceptable personal Runtime closure."""
    _validate_account(account)
    async with AsyncSessionLocal() as db:
        user = await db.scalar(
            select(User).where(User.id == account.user_id, User.org_id == account.org_id)
        )
        if user is None:
            raise FreshRuntimeSmokeCleanupError("fresh smoke user is unavailable")
        if user.user_code != account.work_no or user.sso_subject != account.work_no:
            raise FreshRuntimeSmokeCleanupError("fresh smoke user identity changed")
        profiles = list(
            await db.scalars(
                select(CloudHermesProfile)
                .where(
                    CloudHermesProfile.org_id == account.org_id,
                    CloudHermesProfile.user_id == account.user_id,
                )
                .order_by(CloudHermesProfile.id)
            )
        )
        if not profiles:
            return FreshRuntimeCleanupPlan(
                account=account,
                profile_id=None,
                binding_id=None,
                runtime_id=None,
                namespace=None,
                kservice_name=None,
                pvc_name=None,
                pvc_uid=None,
                pvc_storage_identity_hash=None,
                runtime_root=None,
                runtime_agent_profile_ids=(),
            )
        if len(profiles) != 1 or profiles[0].profile_type != "default_chat_agent":
            raise FreshRuntimeSmokeCleanupError("fresh smoke profile closure is not unique")
        profile = profiles[0]
        bindings = list(
            await db.scalars(
                select(HermesRuntimeBinding)
                .where(
                    HermesRuntimeBinding.runtime_workspace_id == profile.id,
                    HermesRuntimeBinding.org_id == account.org_id,
                    HermesRuntimeBinding.user_id == account.user_id,
                )
                .order_by(HermesRuntimeBinding.id)
            )
        )
        if len(bindings) > 1:
            raise FreshRuntimeSmokeCleanupError("fresh smoke Binding closure is not unique")
        agents = list(
            await db.scalars(
                select(RuntimeAgentProfile)
                .where(
                    RuntimeAgentProfile.runtime_workspace_id == profile.id,
                    RuntimeAgentProfile.org_id == account.org_id,
                    RuntimeAgentProfile.user_id == account.user_id,
                )
                .order_by(RuntimeAgentProfile.id)
            )
        )
        if not bindings:
            return FreshRuntimeCleanupPlan(
                account=account,
                profile_id=int(profile.id),
                binding_id=None,
                runtime_id=None,
                namespace=None,
                kservice_name=None,
                pvc_name=None,
                pvc_uid=None,
                pvc_storage_identity_hash=None,
                runtime_root=None,
                runtime_agent_profile_ids=tuple(int(item.id) for item in agents),
            )
        binding = bindings[0]
        _validate_binding(account=account, profile=profile, binding=binding)
        return FreshRuntimeCleanupPlan(
            account=account,
            profile_id=int(profile.id),
            binding_id=int(binding.id),
            runtime_id=str(binding.runtime_id),
            namespace=str(binding.k8s_namespace),
            kservice_name=str(binding.knative_service),
            pvc_name=str(binding.runtime_root_pvc),
            pvc_uid=(str(binding.runtime_root_pvc_uid) if binding.runtime_root_pvc_uid else None),
            pvc_storage_identity_hash=(
                str(binding.pvc_storage_identity_hash)
                if binding.pvc_storage_identity_hash
                else None
            ),
            runtime_root=Path(str(binding.runtime_root_host_path)),
            runtime_agent_profile_ids=tuple(int(item.id) for item in agents),
        )


def _validate_account(account: FreshRuntimeSmokeAccount) -> None:
    if account.org_id <= 0 or account.user_id <= 0:
        raise FreshRuntimeSmokeCleanupError("fresh smoke account ID is invalid")
    if not _WORK_NO_RE.fullmatch(account.work_no):
        raise FreshRuntimeSmokeCleanupError("fresh smoke work number is invalid")
    if not account.work_no.endswith(account.acceptance_id):
        raise FreshRuntimeSmokeCleanupError("fresh smoke work number does not match acceptance")


def _validate_binding(
    *,
    account: FreshRuntimeSmokeAccount,
    profile: CloudHermesProfile,
    binding: HermesRuntimeBinding,
) -> None:
    key = runtime_resource_key(account.work_no)
    service_name = f"runtime-{key}"
    expected_root = Path(
        runtime_root_host_path(
            runtime_root_path=settings.HERMES_RUNTIME_ROOT,
            org_id=account.org_id,
            resource_key=key,
        )
    ).resolve(strict=False)
    persisted_root = Path(str(binding.runtime_root_host_path or "")).resolve(strict=False)
    expected = (
        binding.org_id == account.org_id
        and binding.user_id == account.user_id
        and binding.runtime_workspace_id == profile.id
        and binding.runtime_id == f"rt-{profile.id}"
        and binding.k8s_namespace == settings.HERMES_RUNTIME_NAMESPACE
        and binding.knative_service == service_name
        and binding.runtime_root_pvc == f"{service_name}-root"
        and binding.runtime_root_host_path
        and persisted_root == expected_root
    )
    if not expected:
        raise FreshRuntimeSmokeCleanupError("fresh smoke Runtime Binding identity changed")


async def _cleanup_kubernetes(
    plan: FreshRuntimeCleanupPlan,
    *,
    timeout_seconds: float,
    kubeconfig: str | None,
) -> None:
    if plan.binding_id is None:
        return
    if not plan.namespace or not plan.kservice_name or not plan.pvc_name:
        raise FreshRuntimeSmokeCleanupError("fresh smoke Kubernetes identity is incomplete")
    kubernetes = KubectlKnativeClient(kubeconfig=kubeconfig)
    if await kubernetes.kservice_exists(namespace=plan.namespace, name=plan.kservice_name):
        service = await kubernetes.get_kservice(
            namespace=plan.namespace,
            name=plan.kservice_name,
        )
        _validate_kservice(service, plan)
        await kubernetes.delete_kservice(namespace=plan.namespace, name=plan.kservice_name)
    await _wait_for_no_kservice_or_claim_pods(
        kubernetes=kubernetes,
        plan=plan,
        timeout_seconds=timeout_seconds,
    )
    await _delete_binding_secrets(kubernetes=kubernetes, plan=plan)
    await _delete_pvc_and_pv(
        kubernetes=kubernetes,
        plan=plan,
        timeout_seconds=timeout_seconds,
    )


def _validate_kservice(service: dict[str, Any], plan: FreshRuntimeCleanupPlan) -> None:
    metadata = service.get("metadata")
    spec = service.get("spec")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    template = spec.get("template") if isinstance(spec, dict) else None
    template_metadata = template.get("metadata") if isinstance(template, dict) else None
    annotations = template_metadata.get("annotations") if isinstance(template_metadata, dict) else None
    if not isinstance(labels, dict) or not isinstance(annotations, dict):
        raise FreshRuntimeSmokeCleanupError("fresh smoke KService metadata is invalid")
    if (
        labels.get("hermes.platform/runtime-id") != plan.runtime_id
        or labels.get("hermes.platform/runtime-binding-type") != _PERSONAL_BINDING_TYPE
        or labels.get("hermes.platform/runtime-binding-id") != str(plan.binding_id)
        or annotations.get("hermes.platform/pvc-storage-identity-hash")
        != plan.pvc_storage_identity_hash
    ):
        raise FreshRuntimeSmokeCleanupError("fresh smoke KService identity changed")


async def _wait_for_no_kservice_or_claim_pods(
    *,
    kubernetes: KubectlKnativeClient,
    plan: FreshRuntimeCleanupPlan,
    timeout_seconds: float,
) -> None:
    assert plan.namespace is not None and plan.kservice_name is not None and plan.pvc_name is not None
    deadline = time.monotonic() + timeout_seconds
    while True:
        exists = await kubernetes.kservice_exists(
            namespace=plan.namespace,
            name=plan.kservice_name,
        )
        pods = await kubernetes.list_persistent_volume_claim_pods(
            namespace=plan.namespace,
            pvc_name=plan.pvc_name,
        )
        if not exists and not pods:
            return
        if time.monotonic() >= deadline:
            raise FreshRuntimeSmokeCleanupError("fresh smoke PVC Pods did not reach zero")
        await asyncio.sleep(1)


async def _delete_binding_secrets(
    *,
    kubernetes: KubectlKnativeClient,
    plan: FreshRuntimeCleanupPlan,
) -> None:
    assert plan.namespace is not None and plan.kservice_name is not None
    names = (
        f"{plan.kservice_name}-api-key",
        platform_control_secret_name(plan.kservice_name),
    )
    for name in names:
        if await kubernetes.secret_exists(namespace=plan.namespace, name=name):
            await kubernetes.delete_secret(namespace=plan.namespace, name=name)
            if await kubernetes.secret_exists(namespace=plan.namespace, name=name):
                raise FreshRuntimeSmokeCleanupError("fresh smoke Runtime Secret still exists")


async def _delete_pvc_and_pv(
    *,
    kubernetes: KubectlKnativeClient,
    plan: FreshRuntimeCleanupPlan,
    timeout_seconds: float,
) -> None:
    assert plan.namespace is not None and plan.pvc_name is not None and plan.pv_name is not None
    pvc_exists = await kubernetes.persistent_volume_claim_exists(
        namespace=plan.namespace,
        name=plan.pvc_name,
    )
    pv_exists = await kubernetes.persistent_volume_exists(name=plan.pv_name)
    if pvc_exists:
        if not plan.pvc_uid or not plan.pvc_storage_identity_hash:
            raise FreshRuntimeSmokeCleanupError("fresh smoke PVC identity is unavailable")
        pvc = await kubernetes.get_persistent_volume_claim(
            namespace=plan.namespace,
            name=plan.pvc_name,
        )
        _validate_pvc(pvc, plan)
        if not pv_exists:
            raise FreshRuntimeSmokeCleanupError("fresh smoke PV is missing before PVC cleanup")
        volume = await kubernetes.get_persistent_volume(name=plan.pv_name)
        _validate_pv_claim(volume, plan)
        await kubernetes.delete_persistent_volume_claim(
            namespace=plan.namespace,
            name=plan.pvc_name,
        )
        await _wait_resource_absent(
            exists=lambda: kubernetes.persistent_volume_claim_exists(
                namespace=plan.namespace,
                name=plan.pvc_name,
            ),
            timeout_seconds=timeout_seconds,
            message="fresh smoke PVC still exists",
        )
        pv_exists = await kubernetes.persistent_volume_exists(name=plan.pv_name)
    if pv_exists:
        if not plan.pvc_uid:
            raise FreshRuntimeSmokeCleanupError("fresh smoke PV identity is unavailable")
        volume = await kubernetes.get_persistent_volume(name=plan.pv_name)
        _validate_pv_claim(volume, plan)
        await kubernetes.delete_persistent_volume(name=plan.pv_name)
        await _wait_resource_absent(
            exists=lambda: kubernetes.persistent_volume_exists(name=plan.pv_name),
            timeout_seconds=timeout_seconds,
            message="fresh smoke PV still exists",
        )


def _validate_pvc(pvc: dict[str, Any], plan: FreshRuntimeCleanupPlan) -> None:
    metadata = pvc.get("metadata")
    spec = pvc.get("spec")
    uid = metadata.get("uid") if isinstance(metadata, dict) else None
    volume_name = spec.get("volumeName") if isinstance(spec, dict) else None
    if (
        uid != plan.pvc_uid
        or volume_name != plan.pv_name
        or runtime_pvc_storage_identity_hash(namespace=str(plan.namespace), pvc_uid=str(uid))
        != plan.pvc_storage_identity_hash
    ):
        raise FreshRuntimeSmokeCleanupError("fresh smoke PVC identity changed")


def _validate_pv_claim(volume: dict[str, Any], plan: FreshRuntimeCleanupPlan) -> None:
    spec = volume.get("spec")
    claim = spec.get("claimRef") if isinstance(spec, dict) else None
    if not isinstance(claim, dict) or (
        claim.get("namespace"), claim.get("name"), claim.get("uid")
    ) != (plan.namespace, plan.pvc_name, plan.pvc_uid):
        raise FreshRuntimeSmokeCleanupError("fresh smoke PV claim identity changed")


async def _wait_resource_absent(
    *,
    exists: Any,
    timeout_seconds: float,
    message: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while await exists():
        if time.monotonic() >= deadline:
            raise FreshRuntimeSmokeCleanupError(message)
        await asyncio.sleep(1)


def _remove_runtime_root(plan: FreshRuntimeCleanupPlan) -> None:
    if plan.runtime_root is None:
        return
    boundary = Path(settings.HERMES_RUNTIME_ROOT).resolve(strict=False)
    root = plan.runtime_root.resolve(strict=False)
    if root == boundary or not root.is_relative_to(boundary):
        raise FreshRuntimeSmokeCleanupError("fresh smoke Runtime root escapes boundary")
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise FreshRuntimeSmokeCleanupError("fresh smoke Runtime root is not a directory")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise FreshRuntimeSmokeCleanupError("safe Runtime root deletion is unavailable")
    for directory, directories, filenames in os.walk(root, followlinks=False):
        for name in [*directories, *filenames]:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise FreshRuntimeSmokeCleanupError("fresh smoke Runtime root contains a symbolic link")
    shutil.rmtree(root)
    if root.exists():
        raise FreshRuntimeSmokeCleanupError("fresh smoke Runtime root still exists")


async def _cleanup_database(plan: FreshRuntimeCleanupPlan) -> dict[str, int]:
    """Delete the known closure in FK order, then reject schema drift."""
    async with AsyncSessionLocal() as db:
        current = await discover_cleanup_plan(plan.account)
        if current != plan:
            raise FreshRuntimeSmokeCleanupError("fresh smoke database identity changed")
        counts: dict[str, int] = {}
        if plan.profile_id is None:
            await _delete_and_count(
                db, counts, "user_role", delete(UserRole).where(UserRole.user_id == plan.account.user_id)
            )
            await _delete_and_count(
                db,
                counts,
                "user",
                delete(User).where(User.id == plan.account.user_id, User.org_id == plan.account.org_id),
            )
            await db.commit()
            return counts

        profile_id = plan.profile_id
        session_ids = list(
            await db.scalars(select(RuntimeSession.id).where(RuntimeSession.runtime_workspace_id == profile_id))
        )
        run_ids = list(
            await db.scalars(select(RuntimeRun.id).where(RuntimeRun.runtime_workspace_id == profile_id))
        )
        task_ids = list(
            await db.scalars(select(ScheduledTask.id).where(ScheduledTask.runtime_workspace_id == profile_id))
        )
        collaboration_ids = list(
            await db.scalars(
                select(ExpertKanbanCollaboration.id).where(
                    ExpertKanbanCollaboration.runtime_workspace_id == profile_id
                )
            )
        )
        if run_ids:
            await _delete_and_count(
                db, counts, "runtime_run_event", delete(RuntimeRunEvent).where(RuntimeRunEvent.run_id.in_(run_ids))
            )
        if task_ids:
            await _delete_and_count(
                db,
                counts,
                "scheduled_task_execution_lease",
                delete(ScheduledTaskExecutionLease).where(ScheduledTaskExecutionLease.task_id.in_(task_ids)),
            )
        if collaboration_ids:
            await _delete_and_count(
                db,
                counts,
                "expert_kanban_collaboration_task",
                delete(ExpertKanbanCollaborationTask).where(
                    ExpertKanbanCollaborationTask.collaboration_id.in_(collaboration_ids)
                ),
            )
        for label, statement in (
            ("chat_file_artifact", delete(ChatFileArtifact).where(ChatFileArtifact.runtime_workspace_id == profile_id)),
            ("chat_log", delete(ChatLog).where(ChatLog.runtime_workspace_id == profile_id)),
            ("model_usage_log", delete(ModelUsageLog).where(ModelUsageLog.runtime_workspace_id == profile_id)),
            ("runtime_audit_log", delete(RuntimeAuditLog).where(or_(RuntimeAuditLog.runtime_workspace_id == profile_id, RuntimeAuditLog.actor_user_id == plan.account.user_id))),
            ("runtime_clarify_request", delete(RuntimeClarifyRequest).where(RuntimeClarifyRequest.runtime_workspace_id == profile_id)),
            ("asset_rollout_target", delete(AssetRolloutTarget).where(AssetRolloutTarget.runtime_workspace_id == profile_id)),
            ("profile_convergence_attempt", delete(ProfileConvergenceAttempt).where(ProfileConvergenceAttempt.runtime_agent_profile_id.in_(plan.runtime_agent_profile_ids))),
            ("profile_convergence_job", delete(ProfileConvergenceJob).where(ProfileConvergenceJob.runtime_workspace_id == profile_id)),
            ("personal_config_bundle_artifact", delete(PersonalConfigBundleArtifact).where(PersonalConfigBundleArtifact.runtime_workspace_id == profile_id)),
            ("profile_skill_binding", delete(ProfileSkillBinding).where(ProfileSkillBinding.runtime_workspace_id == profile_id)),
            ("profile_mcp_binding", delete(ProfileMcpBinding).where(ProfileMcpBinding.runtime_workspace_id == profile_id)),
            ("profile_knowledge_binding", delete(ProfileKnowledgeBinding).where(ProfileKnowledgeBinding.runtime_workspace_id == profile_id)),
            ("runtime_agent_profile_revision", delete(RuntimeAgentProfileRevision).where(RuntimeAgentProfileRevision.runtime_workspace_id == profile_id)),
            ("runtime_kanban_lease_event", delete(RuntimeKanbanLeaseEvent).where(RuntimeKanbanLeaseEvent.runtime_workspace_id == profile_id)),
            ("runtime_kanban_lease", delete(RuntimeKanbanLease).where(RuntimeKanbanLease.runtime_workspace_id == profile_id)),
            ("user_skillhub_installation", delete(UserSkillHubInstallation).where(UserSkillHubInstallation.runtime_workspace_id == profile_id)),
            ("installed_expert_binding", delete(InstalledExpertBinding).where(InstalledExpertBinding.runtime_workspace_id == profile_id)),
            ("expert_profile_operation", delete(ExpertProfileOperation).where(ExpertProfileOperation.runtime_workspace_id == profile_id)),
            ("expert_kanban_collaboration", delete(ExpertKanbanCollaboration).where(ExpertKanbanCollaboration.runtime_workspace_id == profile_id)),
            ("scheduled_task", delete(ScheduledTask).where(ScheduledTask.runtime_workspace_id == profile_id)),
            ("runtime_run", delete(RuntimeRun).where(RuntimeRun.runtime_workspace_id == profile_id)),
            ("runtime_session", delete(RuntimeSession).where(RuntimeSession.runtime_workspace_id == profile_id)),
        ):
            await _delete_and_count(db, counts, label, statement)
        if plan.runtime_agent_profile_ids:
            await _delete_and_count(
                db,
                counts,
                "runtime_agent_profile",
                delete(RuntimeAgentProfile).where(RuntimeAgentProfile.id.in_(plan.runtime_agent_profile_ids)),
            )
        await _delete_expert_package_closure(db, plan, counts)
        if plan.binding_id is not None and plan.runtime_id is not None:
            binding_id = str(plan.binding_id)
            await _delete_and_count(
                db,
                counts,
                "runtime_projection_receipt",
                delete(RuntimeProjectionReceipt).where(
                    RuntimeProjectionReceipt.runtime_id == plan.runtime_id,
                    RuntimeProjectionReceipt.binding_id == binding_id,
                ),
            )
            await _delete_and_count(
                db,
                counts,
                "runtime_admission_audit",
                delete(RuntimeAdmissionAudit).where(
                    RuntimeAdmissionAudit.org_id == plan.account.org_id,
                    RuntimeAdmissionAudit.runtime_id == plan.runtime_id,
                    RuntimeAdmissionAudit.binding_id == binding_id,
                ),
            )
            await _delete_and_count(
                db,
                counts,
                "runtime_bootstrap_intent",
                delete(RuntimeBootstrapIntent).where(
                    RuntimeBootstrapIntent.runtime_binding_type == _PERSONAL_BINDING_TYPE,
                    RuntimeBootstrapIntent.runtime_binding_id == plan.binding_id,
                    RuntimeBootstrapIntent.runtime_id == plan.runtime_id,
                ),
            )
            await _delete_and_count(
                db,
                counts,
                "runtime_platform_control_key",
                delete(RuntimePlatformControlKey).where(
                    RuntimePlatformControlKey.binding_type == _PERSONAL_BINDING_TYPE,
                    RuntimePlatformControlKey.binding_id == plan.binding_id,
                    RuntimePlatformControlKey.runtime_id == plan.runtime_id,
                ),
            )
        registry_state_ids = list(
            await db.scalars(
                select(RuntimeMcpRegistryState.id).where(
                    RuntimeMcpRegistryState.runtime_workspace_id == profile_id
                )
            )
        )
        if registry_state_ids:
            for label, statement in (
                ("runtime_mcp_registry_entry", delete(RuntimeMcpRegistryEntry).where(RuntimeMcpRegistryEntry.runtime_mcp_registry_state_id.in_(registry_state_ids))),
                ("runtime_mcp_registry_job", delete(RuntimeMcpRegistryJob).where(RuntimeMcpRegistryJob.runtime_mcp_registry_state_id.in_(registry_state_ids))),
                ("runtime_mcp_registry_revision", delete(RuntimeMcpRegistryRevision).where(RuntimeMcpRegistryRevision.runtime_mcp_registry_state_id.in_(registry_state_ids))),
                ("runtime_mcp_registry_state", delete(RuntimeMcpRegistryState).where(RuntimeMcpRegistryState.id.in_(registry_state_ids))),
            ):
                await _delete_and_count(db, counts, label, statement)
        if plan.binding_id is not None:
            await _delete_and_count(
                db,
                counts,
                "hermes_runtime_binding",
                delete(HermesRuntimeBinding).where(
                    HermesRuntimeBinding.id == plan.binding_id,
                    HermesRuntimeBinding.runtime_workspace_id == profile_id,
                ),
            )
        await _assert_no_unhandled_associations(
            db,
            plan,
            session_ids=session_ids,
            run_ids=run_ids,
        )
        await _delete_and_count(
            db,
            counts,
            "cloud_hermes_profile",
            delete(CloudHermesProfile).where(CloudHermesProfile.id == profile_id),
        )
        await _delete_and_count(
            db, counts, "user_role", delete(UserRole).where(UserRole.user_id == plan.account.user_id)
        )
        await _delete_and_count(
            db,
            counts,
            "user",
            delete(User).where(User.id == plan.account.user_id, User.org_id == plan.account.org_id),
        )
        await db.commit()
        return counts


async def _delete_expert_package_closure(
    db: AsyncSession,
    plan: FreshRuntimeCleanupPlan,
    counts: dict[str, int],
) -> None:
    if plan.profile_id is None:
        return
    packages = list(
        await db.scalars(
            select(ExpertPackage).where(
                ExpertPackage.org_id == plan.account.org_id,
                ExpertPackage.owner_user_id == plan.account.user_id,
                ExpertPackage.origin_runtime_workspace_id == plan.profile_id,
            )
        )
    )
    if not packages:
        return
    package_ids = [int(package.id) for package in packages]
    remaining_agent_profiles = int(
        (
            await db.scalar(
                select(func.count())
                .select_from(RuntimeAgentProfile)
                .where(RuntimeAgentProfile.expert_package_id.in_(package_ids))
            )
        )
        or 0
    )
    external_installations = int(
        (
            await db.scalar(
                select(func.count())
                .select_from(InstalledExpertBinding)
                .where(InstalledExpertBinding.expert_package_id.in_(package_ids))
            )
        )
        or 0
    )
    template_reviews = int(
        (
            await db.scalar(
                select(func.count())
                .select_from(TemplateReview)
                .where(TemplateReview.expert_package_id.in_(package_ids))
            )
        )
        or 0
    )
    template_versions = int(
        (
            await db.scalar(
                select(func.count())
                .select_from(TemplateVersion)
                .where(TemplateVersion.source_expert_package_id.in_(package_ids))
            )
        )
        or 0
    )
    if remaining_agent_profiles or external_installations or template_reviews or template_versions:
        raise FreshRuntimeSmokeCleanupError(
            "fresh smoke expert package has external references"
        )
    version_ids = list(
        await db.scalars(
            select(ExpertPackageVersion.id).where(
                ExpertPackageVersion.expert_package_id.in_(package_ids)
            )
        )
    )
    if version_ids:
        await _delete_and_count(
            db,
            counts,
            "expert_version_artifact",
            delete(ExpertVersionArtifact).where(
                ExpertVersionArtifact.expert_package_version_id.in_(version_ids)
            ),
        )
        await _delete_and_count(
            db,
            counts,
            "expert_version_knowledge_snapshot",
            delete(ExpertVersionKnowledgeSnapshot).where(
                ExpertVersionKnowledgeSnapshot.expert_package_version_id.in_(version_ids)
            ),
        )
        await _delete_and_count(
            db,
            counts,
            "expert_package_version",
            delete(ExpertPackageVersion).where(ExpertPackageVersion.id.in_(version_ids)),
        )
    await _delete_and_count(
        db,
        counts,
        "expert_package",
        delete(ExpertPackage).where(ExpertPackage.id.in_(package_ids)),
    )


async def _delete_and_count(
    db: AsyncSession,
    counts: dict[str, int],
    label: str,
    statement: Any,
) -> None:
    result = await db.execute(statement)
    affected = int(result.rowcount or 0)
    if affected:
        counts[label] = affected


async def _assert_no_unhandled_associations(
    db: AsyncSession,
    plan: FreshRuntimeCleanupPlan,
    *,
    session_ids: list[int],
    run_ids: list[int],
) -> None:
    """Make a schema addition fail the smoke before it can leave a user behind."""
    scopes: dict[str, set[int]] = {
        "profile_id": {int(plan.profile_id)} if plan.profile_id else set(),
        "runtime_workspace_id": {int(plan.profile_id)} if plan.profile_id else set(),
        "runtime_agent_profile_id": set(plan.runtime_agent_profile_ids),
        "session_id": set(session_ids),
        "run_id": set(run_ids),
        "user_id": {plan.account.user_id},
        "origin_runtime_workspace_id": {int(plan.profile_id)} if plan.profile_id else set(),
        "owner_user_id": {plan.account.user_id},
    }
    for table in Base.metadata.sorted_tables:
        if table.name in {"cloud_hermes_profile", "user", "user_role"}:
            continue
        candidate_scopes = scopes
        if table.name == "model_usage_log":
            candidate_scopes = {
                column_name: values
                for column_name, values in scopes.items()
                if column_name != "run_id"
            }
        clauses = []
        for column_name, values in candidate_scopes.items():
            if values and column_name in table.c:
                clauses.append(table.c[column_name].in_(values))
        if not clauses:
            continue
        statement = select(func.count()).select_from(table).where(or_(*clauses))
        remaining = int((await db.scalar(statement)) or 0)
        if remaining:
            raise FreshRuntimeSmokeCleanupError(
                f"fresh smoke cleanup found unhandled {table.name} records"
            )
    if plan.binding_id is not None and plan.runtime_id is not None:
        binding_id = str(plan.binding_id)
        receipts = int(
            (
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeProjectionReceipt)
                    .where(
                        RuntimeProjectionReceipt.runtime_id == plan.runtime_id,
                        RuntimeProjectionReceipt.binding_id == binding_id,
                    )
                )
            )
            or 0
        )
        audits = int(
            (
                await db.scalar(
                    select(func.count())
                    .select_from(RuntimeAdmissionAudit)
                    .where(
                        RuntimeAdmissionAudit.org_id == plan.account.org_id,
                        RuntimeAdmissionAudit.runtime_id == plan.runtime_id,
                        RuntimeAdmissionAudit.binding_id == binding_id,
                    )
                )
            )
            or 0
        )
        if receipts or audits:
            raise FreshRuntimeSmokeCleanupError(
                "fresh smoke cleanup left Runtime evidence records"
            )


async def _verify_database_absent(plan: FreshRuntimeCleanupPlan) -> None:
    async with AsyncSessionLocal() as db:
        user = await db.scalar(
            select(User.id).where(User.id == plan.account.user_id, User.org_id == plan.account.org_id)
        )
        profiles = int(
            (
                await db.scalar(
                    select(func.count())
                    .select_from(CloudHermesProfile)
                    .where(
                        CloudHermesProfile.org_id == plan.account.org_id,
                        CloudHermesProfile.user_id == plan.account.user_id,
                    )
                )
            )
            or 0
        )
    if user is not None or profiles:
        raise FreshRuntimeSmokeCleanupError("fresh smoke database records still exist")


__all__ = [
    "FreshRuntimeSmokeAccount",
    "FreshRuntimeSmokeCleanupError",
    "acceptance_work_no",
    "cleanup_fresh_runtime",
    "new_acceptance_id",
    "write_checkpoint",
]
