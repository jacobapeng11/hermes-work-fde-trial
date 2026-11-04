"""Fail-closed cleanup for explicitly confirmed failed expert Profiles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    CloudHermesProfile,
    ExpertPackage,
    ExpertProfileOperation,
    HermesRuntimeBinding,
    RuntimeAgentProfile,
    RuntimeRun,
    RuntimeSession,
    User,
)
from app.services.expert_profile_cleanup import FailedExpertProfileCleanupService
from app.services.runtime_activity import ACTIVE_RUN_STATUSES
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec


class FailedExpertCleanupRejected(RuntimeError):
    pass


class ExpertCleanupUninstaller(Protocol):
    async def uninstall(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        endpoint: RuntimeEndpoint,
    ) -> None: ...


@dataclass(frozen=True)
class FailedExpertCleanupIdentity:
    org_id: int
    user_id: int
    runtime_workspace_id: int


@dataclass(frozen=True)
class FailedExpertCleanupItem:
    runtime_agent_profile_id: int
    hermes_profile_name: str
    status: str
    operation_step: str | None
    decision: str
    reason: str


@dataclass(frozen=True)
class FailedExpertCleanupReport:
    mode: str
    identity: FailedExpertCleanupIdentity
    candidates: tuple[FailedExpertCleanupItem, ...]
    excluded: tuple[FailedExpertCleanupItem, ...]
    expected_effects: tuple[str, ...] = (
        "delete Runtime expert Profile and workspace projection",
        "tombstone Backend expert Profile and clear execution binding",
        "preserve historical sessions, runs, packages, versions, and operations",
    )
    results: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FailedExpertCleanupService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        hermes_profiles: HermesProfileAdapter,
        orchestrator_factory: Callable[[AsyncSession], ExpertCleanupUninstaller]
        | None = None,
    ) -> None:
        self.db = db
        self.hermes_profiles = hermes_profiles
        self.codec = RuntimeApiKeyCodec()
        self.orchestrator_factory = orchestrator_factory or (
            lambda session: FailedExpertProfileCleanupService(
                session,
                hermes_profiles=hermes_profiles,
            )
        )

    async def dry_run(
        self,
        *,
        user_name: str,
        stale_before: datetime,
    ) -> FailedExpertCleanupReport:
        user, workspace, binding = await self._resolve_identity(user_name)
        endpoint = self._endpoint(binding)
        profiles = list(
            (
                await self.db.scalars(
                    select(RuntimeAgentProfile)
                    .where(
                        RuntimeAgentProfile.org_id == user.org_id,
                        RuntimeAgentProfile.user_id == user.id,
                        RuntimeAgentProfile.runtime_workspace_id == workspace.id,
                        RuntimeAgentProfile.profile_kind == "expert",
                        RuntimeAgentProfile.status != "deleted",
                    )
                    .order_by(RuntimeAgentProfile.id)
                )
            ).all()
        )
        candidates: list[FailedExpertCleanupItem] = []
        excluded: list[FailedExpertCleanupItem] = []
        for profile in profiles:
            item = await self._classify(
                profile,
                endpoint=endpoint,
                stale_before=stale_before,
            )
            (candidates if item.decision == "candidate" else excluded).append(item)
        return FailedExpertCleanupReport(
            mode="dry-run",
            identity=FailedExpertCleanupIdentity(
                org_id=int(user.org_id),
                user_id=int(user.id),
                runtime_workspace_id=int(workspace.id),
            ),
            candidates=tuple(candidates),
            excluded=tuple(excluded),
        )

    async def apply(
        self,
        *,
        user_name: str,
        identity: FailedExpertCleanupIdentity,
        profile_ids: list[int],
        stale_before: datetime,
    ) -> FailedExpertCleanupReport:
        if not profile_ids or any(item <= 0 for item in profile_ids):
            raise FailedExpertCleanupRejected("explicit positive profile IDs are required")
        if len(set(profile_ids)) != len(profile_ids):
            raise FailedExpertCleanupRejected("duplicate profile IDs are not allowed")
        plan = await self.dry_run(
            user_name=user_name,
            stale_before=stale_before,
        )
        if plan.identity != identity:
            raise FailedExpertCleanupRejected("cleanup identity changed after dry-run")
        candidate_ids = {
            item.runtime_agent_profile_id for item in plan.candidates
        }
        if not set(profile_ids).issubset(candidate_ids):
            raise FailedExpertCleanupRejected(
                "apply contains a profile that is no longer an eligible candidate"
            )
        binding = await self.db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.org_id == identity.org_id,
                HermesRuntimeBinding.user_id == identity.user_id,
                HermesRuntimeBinding.runtime_workspace_id
                == identity.runtime_workspace_id,
                HermesRuntimeBinding.status == "ready",
            )
        )
        if binding is None:
            raise FailedExpertCleanupRejected("Runtime binding is not ready")
        endpoint = self._endpoint(binding)
        results: list[dict[str, Any]] = []
        for profile_id in profile_ids:
            try:
                profile = await self.db.scalar(
                    select(RuntimeAgentProfile)
                    .where(
                        RuntimeAgentProfile.id == profile_id,
                        RuntimeAgentProfile.org_id == identity.org_id,
                        RuntimeAgentProfile.user_id == identity.user_id,
                        RuntimeAgentProfile.runtime_workspace_id
                        == identity.runtime_workspace_id,
                        RuntimeAgentProfile.profile_kind == "expert",
                    )
                    .with_for_update()
                )
                if profile is None:
                    raise FailedExpertCleanupRejected("cleanup profile identity drifted")
                operation = await self.db.scalar(
                    select(ExpertProfileOperation)
                    .where(
                        ExpertProfileOperation.runtime_agent_profile_id
                        == profile_id,
                        ExpertProfileOperation.status == "running",
                    )
                    .order_by(ExpertProfileOperation.created_at.desc())
                    .with_for_update()
                )
                if operation is not None:
                    activity_at = (
                        operation.updated_at
                        or operation.started_at
                        or operation.created_at
                    )
                    if not self._is_stale(activity_at, stale_before):
                        raise FailedExpertCleanupRejected(
                            "active expert operation is not stale"
                        )
                    operation.status = "failed"
                    operation.active_profile_key = None
                    operation.error_code = "operator_cleanup_requested"
                    operation.error_message = "operator requested failed expert cleanup"
                    operation.finished_at = datetime.now(timezone.utc)
                    await self.db.commit()
                await self.orchestrator_factory(self.db).uninstall(
                    principal=Principal(
                        org_id=identity.org_id,
                        user_id=identity.user_id,
                    ),
                    runtime_workspace_id=identity.runtime_workspace_id,
                    runtime_agent_profile_id=profile_id,
                    endpoint=endpoint,
                )
                self.db.expire_all()
                verified = await self.db.get(RuntimeAgentProfile, profile_id)
                active = await self.db.scalar(
                    select(ExpertProfileOperation.id).where(
                        ExpertProfileOperation.runtime_agent_profile_id
                        == profile_id,
                        ExpertProfileOperation.status == "running",
                    )
                )
                if verified is None or verified.status != "deleted" or active is not None:
                    raise FailedExpertCleanupRejected(
                        "cleanup verification did not converge"
                    )
                results.append({"runtime_agent_profile_id": profile_id, "status": "deleted"})
            except Exception as exc:
                results.append(
                    {
                        "runtime_agent_profile_id": profile_id,
                        "status": "failed",
                        "error": type(exc).__name__,
                    }
                )
                break
        return FailedExpertCleanupReport(
            mode="apply",
            identity=identity,
            candidates=plan.candidates,
            excluded=plan.excluded,
            results=tuple(results),
        )

    async def _resolve_identity(
        self,
        user_name: str,
    ) -> tuple[User, CloudHermesProfile, HermesRuntimeBinding]:
        name = user_name.strip()
        users = list(
            (
                await self.db.scalars(
                    select(User).where(User.name == name).order_by(User.id)
                )
            ).all()
        )
        if len(users) != 1:
            raise FailedExpertCleanupRejected(
                f"expected exactly one user named {name!r}; found {len(users)}"
            )
        user = users[0]
        if user.status != "active":
            raise FailedExpertCleanupRejected("target user is not active")
        workspaces = list(
            (
                await self.db.scalars(
                    select(CloudHermesProfile).where(
                        CloudHermesProfile.org_id == user.org_id,
                        CloudHermesProfile.user_id == user.id,
                        CloudHermesProfile.profile_type == "default_chat_agent",
                        CloudHermesProfile.status == "active",
                    )
                )
            ).all()
        )
        if len(workspaces) != 1:
            raise FailedExpertCleanupRejected(
                "target personal Runtime workspace is missing or ambiguous"
            )
        workspace = workspaces[0]
        bindings = list(
            (
                await self.db.scalars(
                    select(HermesRuntimeBinding).where(
                        HermesRuntimeBinding.org_id == user.org_id,
                        HermesRuntimeBinding.user_id == user.id,
                        HermesRuntimeBinding.runtime_workspace_id == workspace.id,
                        HermesRuntimeBinding.status == "ready",
                    )
                )
            ).all()
        )
        if len(bindings) != 1:
            raise FailedExpertCleanupRejected(
                "target Runtime binding is missing, ambiguous, or not ready"
            )
        return user, workspace, bindings[0]

    async def _classify(
        self,
        profile: RuntimeAgentProfile,
        *,
        endpoint: RuntimeEndpoint,
        stale_before: datetime,
    ) -> FailedExpertCleanupItem:
        operation = await self.db.scalar(
            select(ExpertProfileOperation)
            .where(
                ExpertProfileOperation.runtime_agent_profile_id == profile.id,
                ExpertProfileOperation.status == "running",
            )
            .order_by(ExpertProfileOperation.created_at.desc())
        )
        step = str(operation.step) if operation is not None else None
        reason = "eligible_failed_profile"
        if profile.status == "ready":
            reason = "ready"
        elif profile.status == "error":
            if operation is not None and not self._is_stale(
                operation.updated_at or operation.started_at or operation.created_at,
                stale_before,
            ):
                reason = "active_operation"
        elif profile.status in {"provisioning", "updating", "deleting"}:
            if operation is None or not self._is_stale(
                operation.updated_at or operation.started_at or operation.created_at,
                stale_before,
            ):
                reason = "not_stale"
            else:
                reason = "eligible_stale_operation"
        else:
            reason = "unsupported_status"
        if reason.startswith("eligible"):
            package = (
                await self.db.get(ExpertPackage, int(profile.expert_package_id))
                if profile.expert_package_id is not None
                else None
            )
            if (
                profile.ownership_type == "self_created"
                and package is not None
                and package.status == "published"
            ):
                reason = "blocked_published"
        if reason.startswith("eligible"):
            active_run = await self.db.scalar(
                select(RuntimeRun.id)
                .join(RuntimeSession, RuntimeSession.id == RuntimeRun.session_id)
                .where(
                    RuntimeSession.runtime_agent_profile_id == profile.id,
                    RuntimeRun.status.in_(tuple(sorted(ACTIVE_RUN_STATUSES))),
                )
                .limit(1)
            )
            if active_run is not None:
                reason = "active_run"
        if reason.startswith("eligible"):
            try:
                if await self.hermes_profiles.has_nonterminal_kanban_task(
                    endpoint,
                    profile_name=str(profile.hermes_profile_name),
                ):
                    reason = "active_kanban"
            except Exception:
                reason = "runtime_state_unavailable"
        decision = "candidate" if reason.startswith("eligible") else "excluded"
        return FailedExpertCleanupItem(
            runtime_agent_profile_id=int(profile.id),
            hermes_profile_name=str(profile.hermes_profile_name),
            status=str(profile.status),
            operation_step=step,
            decision=decision,
            reason=reason,
        )

    @staticmethod
    def _is_stale(value: datetime | None, stale_before: datetime) -> bool:
        if value is None:
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if stale_before.tzinfo is None:
            stale_before = stale_before.replace(tzinfo=timezone.utc)
        return value <= stale_before

    def _endpoint(self, binding: HermesRuntimeBinding) -> RuntimeEndpoint:
        if not binding.endpoint_base_url or not binding.endpoint_host_header:
            raise FailedExpertCleanupRejected("Runtime endpoint is incomplete")
        return RuntimeEndpoint(
            runtime_workspace_id=int(binding.runtime_workspace_id),
            runtime_id=str(binding.runtime_id),
            endpoint_base_url=str(binding.endpoint_base_url),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_key=self.codec.decrypt(str(binding.api_key_encrypted)),
        )
