from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.hermes_runtime.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
    RuntimeControlBusy,
)
from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.toolset_policy import build_platform_cli_toolsets
from app.models import (
    HermesRuntimeBinding,
    ProfileConvergenceAttempt,
    ProfileConvergenceJob,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryState,
)
from app.schemas.asset_projection import (
    ProfileArtifactManifestEntry,
    ProfileCapabilityTarget,
    ProfileComponentDigests,
    ProfileProjectionApplyRequest,
    ProfileProjectionApplyResponse,
)
from app.services.asset_rollout import refresh_rollout_targets_for_profile
from app.services.runtime_workspace import RuntimeApiKeyCodec


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClaimedProfileProjection:
    job_id: int
    attempt_id: int
    lease_epoch: int
    target_revision: int
    request: ProfileProjectionApplyRequest
    endpoint: RuntimeEndpoint


class ProfileProjectionClient(Protocol):
    async def apply_profile_projection(
        self,
        endpoint: RuntimeEndpoint,
        request: ProfileProjectionApplyRequest,
    ) -> ProfileProjectionApplyResponse: ...

    async def get_profile_projection_status(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_agent_profile_id: int,
    ) -> ProfileProjectionApplyResponse: ...


class ProfileProjectionTargetResolver(Protocol):
    async def resolve(
        self,
        db: AsyncSession,
        *,
        profile: RuntimeAgentProfile,
    ) -> tuple[int, RuntimeEndpoint]: ...


class SqlAlchemyProfileProjectionTargetResolver:
    async def resolve(
        self,
        db: AsyncSession,
        *,
        profile: RuntimeAgentProfile,
    ) -> tuple[int, RuntimeEndpoint]:
        binding = await db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id
                == profile.runtime_workspace_id,
                HermesRuntimeBinding.org_id == profile.org_id,
                HermesRuntimeBinding.user_id == profile.user_id,
                HermesRuntimeBinding.status == "ready",
            )
        )
        if (
            binding is None
            or not binding.endpoint_base_url
            or not binding.endpoint_host_header
        ):
            raise ProfileConvergenceError(
                "RUNTIME_NOT_READY",
                retryable=True,
            )
        return int(binding.id), RuntimeEndpoint(
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_id=str(binding.runtime_id),
            endpoint_base_url=str(binding.endpoint_base_url),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_key=RuntimeApiKeyCodec().decrypt(str(binding.api_key_encrypted)),
            timeout_seconds=120,
        )


class ProfileConvergenceError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ProfileConvergenceWorker:
    """Lease and converge latest complete Profile revisions without dual writes."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        client: ProfileProjectionClient | None = None,
        target_resolver: ProfileProjectionTargetResolver | None = None,
        worker_id: str,
        lease_seconds: int = 180,
        concurrency: int = 4,
        batch_size: int = 16,
    ) -> None:
        self.session_factory = session_factory
        self.client = client or HermesDashboardProtocolAdapter()
        self.target_resolver = (
            target_resolver or SqlAlchemyProfileProjectionTargetResolver()
        )
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.concurrency = concurrency
        self.batch_size = batch_size

    async def reconcile_once(self) -> int:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            ids = list(
                await db.scalars(
                    select(ProfileConvergenceJob.id)
                    .where(
                        ProfileConvergenceJob.available_at <= now,
                        or_(
                            ProfileConvergenceJob.status == "waiting",
                            (
                                (ProfileConvergenceJob.status == "updating")
                                & (
                                    (ProfileConvergenceJob.lease_expires_at.is_(None))
                                    | (
                                        ProfileConvergenceJob.lease_expires_at
                                        <= now
                                    )
                                )
                            ),
                        ),
                    )
                    .order_by(
                        ProfileConvergenceJob.available_at.asc(),
                        ProfileConvergenceJob.id.asc(),
                    )
                    .limit(self.batch_size)
                )
            )
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(job_id: int) -> bool:
            async with semaphore:
                return await self._process(int(job_id))

        outcomes = await asyncio.gather(
            *(process(int(job_id)) for job_id in ids),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning(
                    "profile_convergence_worker_error",
                    extra={"error_type": type(outcome).__name__},
                )
        return sum(outcome is True for outcome in outcomes)

    async def _process(self, job_id: int) -> bool:
        claimed = await self._claim(job_id)
        if claimed is None:
            return False
        try:
            response = await self.client.apply_profile_projection(
                claimed.endpoint,
                claimed.request,
            )
            if response.status not in {"applied", "applied_degraded"}:
                error_code = response.error_code or "PROFILE_PROJECTION_FAILED"
                raise ProfileConvergenceError(
                    error_code,
                    retryable=error_code
                    in {
                        "PROFILE_ARTIFACT_DOWNLOAD_FAILED",
                        "PROFILE_PROJECTION_FAILED",
                    },
                )
            self._validate_response(claimed, response)
        except ProfileConvergenceError as exc:
            await self._fail(claimed, exc)
            return True
        except HermesProtocolError as exc:
            code = str(exc).strip()[:128] or "HERMES_PROTOCOL_ERROR"
            if code in {
                "PROFILE_APPLIED_REVISION_STALE",
                "PROFILE_EFFECTIVE_GENERATION_STALE",
            }:
                recovered, recovery_error = await self._recover_stale_generation(
                    claimed
                )
                if recovered:
                    return True
                await self._fail(
                    claimed,
                    recovery_error
                    or ProfileConvergenceError(
                        "PROFILE_PROJECTION_STATE_DIVERGED",
                        retryable=False,
                    ),
                )
                return True
            await self._fail(
                claimed,
                ProfileConvergenceError(
                    code,
                    retryable=(
                        isinstance(exc, RuntimeControlBusy)
                        or exc.status_code is None
                        or exc.status_code >= 500
                    ),
                ),
            )
            return True
        except Exception as exc:
            await self._fail(
                claimed,
                ProfileConvergenceError(
                    type(exc).__name__.upper()[:128],
                    retryable=True,
                ),
            )
            return True
        await self._complete(claimed, response)
        return True

    async def _recover_stale_generation(
        self,
        claimed: ClaimedProfileProjection,
    ) -> tuple[bool, ProfileConvergenceError | None]:
        try:
            observed = await self.client.get_profile_projection_status(
                claimed.endpoint,
                runtime_agent_profile_id=(
                    claimed.request.runtime_agent_profile_id
                ),
            )
        except Exception:
            return False, ProfileConvergenceError(
                "PROFILE_PROJECTION_STATUS_UNAVAILABLE",
                retryable=True,
            )

        now = datetime.utcnow()
        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(
                        ProfileConvergenceJob,
                        ProfileConvergenceAttempt,
                        RuntimeAgentProfile,
                    )
                    .join(
                        ProfileConvergenceAttempt,
                        ProfileConvergenceAttempt.profile_convergence_job_id
                        == ProfileConvergenceJob.id,
                    )
                    .join(
                        RuntimeAgentProfile,
                        RuntimeAgentProfile.id
                        == ProfileConvergenceJob.runtime_agent_profile_id,
                    )
                    .where(
                        ProfileConvergenceJob.id == claimed.job_id,
                        ProfileConvergenceAttempt.id == claimed.attempt_id,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return False, ProfileConvergenceError(
                    "PROFILE_PROJECTION_RECOVERY_LOST_LEASE",
                    retryable=True,
                )
            job, attempt, profile = row
            if not self._owns(job, claimed):
                return False, ProfileConvergenceError(
                    "PROFILE_PROJECTION_RECOVERY_LOST_LEASE",
                    retryable=True,
                )
            control_plane_revision = int(profile.applied_revision or 0)
            observed_revision = int(observed.applied_revision or 0)
            applied_revision = (
                observed_revision
                if observed_revision > control_plane_revision
                else control_plane_revision
            )
            revision = (
                await db.scalar(
                    select(RuntimeAgentProfileRevision).where(
                        RuntimeAgentProfileRevision.runtime_agent_profile_id
                        == profile.id,
                        RuntimeAgentProfileRevision.revision == applied_revision,
                    )
                )
                if applied_revision > 0
                else None
            )
            expected_digests = (
                ProfileComponentDigests(
                    profile=str(revision.profile_digest),
                    skill=str(revision.skill_digest),
                    mcp=str(revision.mcp_digest),
                    knowledge=str(revision.knowledge_digest),
                )
                if revision is not None
                else None
            )
            safe_to_repair = (
                observed.status == "applied"
                and observed.runtime_agent_profile_id == int(profile.id)
                and control_plane_revision <= observed_revision
                and observed_revision <= int(profile.desired_revision or 0)
                and observed.digests is not None
                and observed.digests == expected_digests
                and revision is not None
                and (
                    observed_revision > control_plane_revision
                    or str(profile.applied_digest or "")
                    == str(revision.profile_digest)
                )
                and observed.effective_generation
                >= int(profile.effective_generation or 0)
            )
            if not safe_to_repair:
                return False, ProfileConvergenceError(
                    "PROFILE_PROJECTION_STATE_DIVERGED",
                    retryable=False,
                )

            profile.applied_revision = observed_revision
            profile.applied_digest = str(revision.profile_digest)
            profile.effective_generation = int(observed.effective_generation)
            profile.capability_health = [
                item.model_dump(mode="json") for item in observed.capabilities
            ]
            attempt.status = "waiting"
            attempt.stage = "supervisor_reconcile"
            attempt.completed_at = now
            attempt.evidence = {
                "recovery": "effective_generation",
                "observed_applied_revision": observed.applied_revision,
                "observed_effective_generation": observed.effective_generation,
                "observed_digests": observed.digests.model_dump(mode="json"),
            }
            job.status = "waiting"
            job.stage = "supervisor_reconcile"
            job.available_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = None
            job.last_error_message = None
            await db.commit()
        return True, None

    async def _claim(self, job_id: int) -> ClaimedProfileProjection | None:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            job = await db.scalar(
                select(ProfileConvergenceJob)
                .where(ProfileConvergenceJob.id == job_id)
                .with_for_update()
            )
            if job is None or job.available_at > now:
                return None
            if job.status == "updating" and (
                job.lease_expires_at is None or job.lease_expires_at > now
            ):
                return None
            if job.status not in {"waiting", "updating"}:
                return None
            profile = await db.scalar(
                select(RuntimeAgentProfile)
                .where(RuntimeAgentProfile.id == job.runtime_agent_profile_id)
                .with_for_update()
            )
            if profile is None or profile.status == "deleted":
                job.status = "failed"
                job.last_error_code = "PROFILE_NOT_FOUND"
                job.last_error_message = "Profile is unavailable"
                await db.commit()
                return None
            if profile.status != "active":
                # Lifecycle denial is not projection health. A disabled
                # Profile remains durable but cannot start another apply.
                return None
            target_revision = int(profile.desired_revision or 0)
            if target_revision <= 0 or int(job.target_revision) > target_revision:
                job.status = "failed"
                job.last_error_code = "PROFILE_REVISION_ORDER_INVALID"
                await db.commit()
                return None
            if int(job.target_revision) < target_revision:
                job.target_revision = target_revision
            revision = await db.scalar(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.runtime_agent_profile_id
                    == profile.id,
                    RuntimeAgentProfileRevision.revision == target_revision,
                )
            )
            if revision is None:
                job.status = "failed"
                job.last_error_code = "PROFILE_REVISION_NOT_FOUND"
                job.last_error_message = "Desired revision is unavailable"
                await db.commit()
                return None
            if (
                int(profile.applied_revision or 0) > target_revision
                or str(profile.desired_digest or "")
                != str(revision.profile_digest)
            ):
                job.status = "failed"
                job.last_error_code = "PROFILE_REVISION_ORDER_INVALID"
                await db.commit()
                return None
            mcps = list(dict(revision.snapshot or {}).get("mcps") or [])
            registry_failure = await self._mcp_registry_failure(
                db,
                profile=profile,
            )
            if registry_failure is not None:
                job.status = "failed"
                job.stage = "mcp_registry"
                job.available_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error_code = registry_failure
                job.last_error_message = registry_failure
                await db.commit()
                return None
            if not await self._mcp_registry_ready(
                db,
                profile=profile,
                mcps=mcps,
            ):
                job.status = "waiting"
                job.stage = "mcp_registry"
                job.available_at = now + timedelta(seconds=1)
                job.last_error_code = "MCP_REGISTRY_PENDING"
                job.last_error_message = "MCP_REGISTRY_PENDING"
                await db.commit()
                return None
            try:
                binding_id, endpoint = await self.target_resolver.resolve(
                    db,
                    profile=profile,
                )
            except ProfileConvergenceError as exc:
                job.attempt_count = int(job.attempt_count or 0) + 1
                job.status = "waiting" if exc.retryable else "failed"
                job.stage = "resolve_runtime"
                job.available_at = (
                    now
                    + timedelta(
                        seconds=min(
                            60,
                            2 ** min(int(job.attempt_count), 5),
                        )
                    )
                    if exc.retryable
                    else now
                )
                job.last_error_code = exc.code
                job.last_error_message = exc.code
                db.add(
                    ProfileConvergenceAttempt(
                        profile_convergence_job_id=int(job.id),
                        runtime_agent_profile_id=int(profile.id),
                        attempt_number=int(job.attempt_count),
                        target_revision=target_revision,
                        observed_applied_revision=int(
                            profile.applied_revision or 0
                        ),
                        status="waiting" if exc.retryable else "failed",
                        stage="resolve_runtime",
                        error_code=exc.code,
                        error_message=exc.code,
                        started_at=now,
                        completed_at=now,
                    )
                )
                await db.commit()
                return None
            job.status = "updating"
            job.stage = "supervisor_apply"
            job.lease_owner = self.worker_id
            job.lease_epoch = int(job.lease_epoch or 0) + 1
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.attempt_count = int(job.attempt_count or 0) + 1
            job.last_error_code = None
            job.last_error_message = None
            attempt = ProfileConvergenceAttempt(
                profile_convergence_job_id=int(job.id),
                runtime_agent_profile_id=int(profile.id),
                attempt_number=int(job.attempt_count),
                target_revision=target_revision,
                observed_applied_revision=int(profile.applied_revision or 0),
                status="updating",
                stage="supervisor_apply",
                started_at=now,
            )
            db.add(attempt)
            await db.flush()
            request = self._request(
                profile=profile,
                revision=revision,
                binding_id=binding_id,
                endpoint=endpoint,
            )
            claimed = ClaimedProfileProjection(
                job_id=int(job.id),
                attempt_id=int(attempt.id),
                lease_epoch=int(job.lease_epoch),
                target_revision=target_revision,
                request=request,
                endpoint=endpoint,
            )
            await db.commit()
            return claimed

    @staticmethod
    def _request(
        *,
        profile: RuntimeAgentProfile,
        revision: RuntimeAgentProfileRevision,
        binding_id: int,
        endpoint: RuntimeEndpoint,
    ) -> ProfileProjectionApplyRequest:
        snapshot = dict(revision.snapshot or {})
        definition = dict(snapshot.get("profile") or {})
        knowledge = dict(snapshot.get("knowledge") or {})
        skills = list(snapshot.get("skills") or [])
        mcps = list(snapshot.get("mcps") or [])
        definition.update(
            {
                "enterprise_knowledge_enabled": bool(
                    knowledge.get("enterprise_knowledge_enabled")
                ),
                "personal_knowledge_base_ids": list(
                    knowledge.get("personal_knowledge_base_ids") or []
                ),
                "platform_toolsets": {
                    "cli": build_platform_cli_toolsets(
                        mcp_toolsets=(
                            str(item.get("toolset_key"))
                            for item in mcps
                            if isinstance(item, dict) and item.get("toolset_key")
                        )
                    )
                },
                "mcp_servers": {
                    str(item["server_slug"]): ProfileConvergenceWorker._mcp_server_config(
                        item
                    )
                    for item in mcps
                    if isinstance(item, dict)
                    and item.get("server_slug")
                    and item.get("transport")
                },
            }
        )
        artifacts = [
            ProfileArtifactManifestEntry(
                asset_type="skill",
                asset_id=int(item["skill_id"]),
                version_id=int(item["skill_version_id"]),
                storage_uri=str(item["package_uri"]),
                sha256=str(item["package_sha256"]),
                target_relative_path=(
                    f"skills/releases/{int(item['skill_id'])}/current"
                ),
            )
            for item in skills
            if isinstance(item, dict)
        ]
        capabilities = [
            ProfileCapabilityTarget(
                capability_type="skill",
                capability_id=int(item["skill_id"]),
                target_version_id=int(item["skill_version_id"]),
                desired_digest=f"sha256:{str(item['package_sha256'])}",
            )
            for item in skills
            if isinstance(item, dict)
        ]
        capabilities.extend(
            ProfileCapabilityTarget(
                capability_type="mcp",
                capability_id=int(item["mcp_id"]),
                target_version_id=int(item["mcp_version_id"]),
            )
            for item in mcps
            if isinstance(item, dict)
        )
        capabilities.extend(
            ProfileCapabilityTarget(
                capability_type="knowledge_base",
                capability_id=int(knowledge_base_id),
            )
            for knowledge_base_id in knowledge.get(
                "personal_knowledge_base_ids", []
            )
        )
        operation_id = (
            f"profile-{int(profile.id)}-revision-{int(revision.revision)}-"
            f"{str(revision.profile_digest).removeprefix('sha256:')[:16]}"
        )
        return ProfileProjectionApplyRequest(
            operation_id=operation_id,
            runtime_id=endpoint.runtime_id,
            binding_id=binding_id,
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_agent_profile_id=int(profile.id),
            hermes_profile_name=str(profile.hermes_profile_name),
            desired_revision=int(revision.revision),
            expected_applied_revision=int(profile.applied_revision or 0),
            expected_effective_generation=int(profile.effective_generation or 0),
            digests=ProfileComponentDigests(
                profile=str(revision.profile_digest),
                skill=str(revision.skill_digest),
                mcp=str(revision.mcp_digest),
                knowledge=str(revision.knowledge_digest),
            ),
            profile_config=definition,
            artifacts=artifacts,
            capabilities=capabilities,
        )

    @staticmethod
    def _mcp_server_config(item: dict) -> dict:
        transport = str(item["transport"])
        config = dict(item.get("connection_config") or {})
        credential_target = "env" if transport == "stdio" else "headers"
        existing = config.get(credential_target)
        if existing is not None and not isinstance(existing, dict):
            raise ProfileConvergenceError(
                "MCP_CREDENTIAL_CONFIG_INVALID",
                retryable=False,
            )
        credential_values = dict(item.get("credential_refs") or {})
        if credential_values:
            config[credential_target] = {
                **dict(existing or {}),
                **credential_values,
            }
        config["transport"] = transport
        config["enabled"] = True
        return config

    @staticmethod
    async def _mcp_registry_failure(
        db: AsyncSession,
        *,
        profile: RuntimeAgentProfile,
    ) -> str | None:
        state = await db.scalar(
            select(RuntimeMcpRegistryState).where(
                RuntimeMcpRegistryState.runtime_workspace_id
                == profile.runtime_workspace_id,
                RuntimeMcpRegistryState.org_id == profile.org_id,
                RuntimeMcpRegistryState.user_id == profile.user_id,
            )
        )
        if state is None or state.status != "failed":
            return None
        return str(
            state.last_error_code
            or state.last_error_message
            or "MCP_REGISTRY_FAILED"
        )

    @staticmethod
    async def _mcp_registry_ready(
        db: AsyncSession,
        *,
        profile: RuntimeAgentProfile,
        mcps: list[object],
    ) -> bool:
        expected = {
            (int(item["mcp_id"]), int(item["mcp_version_id"]))
            for item in mcps
            if isinstance(item, dict)
        }
        if not expected:
            return True
        state = await db.scalar(
            select(RuntimeMcpRegistryState).where(
                RuntimeMcpRegistryState.runtime_workspace_id
                == profile.runtime_workspace_id,
                RuntimeMcpRegistryState.org_id == profile.org_id,
                RuntimeMcpRegistryState.user_id == profile.user_id,
            )
        )
        if (
            state is None
            or state.status != "ready"
            or int(state.applied_revision or 0)
            != int(state.desired_revision or 0)
        ):
            return False
        ready = set(
            await db.execute(
                select(
                    RuntimeMcpRegistryEntry.mcp_id,
                    RuntimeMcpRegistryEntry.mcp_version_id,
                ).where(
                    RuntimeMcpRegistryEntry.runtime_mcp_registry_state_id
                    == state.id,
                    RuntimeMcpRegistryEntry.status == "ready",
                )
            )
        )
        return expected.issubset(
            {(int(mcp_id), int(version_id)) for mcp_id, version_id in ready}
        )

    @staticmethod
    def _validate_response(
        claimed: ClaimedProfileProjection,
        response: ProfileProjectionApplyResponse,
    ) -> None:
        request = claimed.request
        requested_capabilities = {
            (item.capability_type, item.capability_id, item.target_version_id)
            for item in request.capabilities
        }
        observed_capabilities = {
            (item.capability_type, item.capability_id, item.target_version_id)
            for item in response.capabilities
        }
        identity_matches = (
            response.operation_id == request.operation_id
            and response.runtime_agent_profile_id == request.runtime_agent_profile_id
            and response.desired_revision == request.desired_revision
            and response.applied_revision == request.desired_revision
            and response.digests == request.digests
            and observed_capabilities == requested_capabilities
        )
        if (
            not identity_matches
            or (
                response.effective_generation
                != request.expected_effective_generation + 1
                and not (
                    response.status in {"applied", "applied_degraded"}
                    and response.effective_generation
                    >= request.expected_effective_generation
                )
            )
        ):
            raise ProfileConvergenceError(
                "PROFILE_PROJECTION_RECEIPT_MISMATCH",
                retryable=False,
            )

    async def _complete(
        self,
        claimed: ClaimedProfileProjection,
        response: ProfileProjectionApplyResponse,
    ) -> None:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(
                        ProfileConvergenceJob,
                        ProfileConvergenceAttempt,
                        RuntimeAgentProfile,
                    )
                    .join(
                        ProfileConvergenceAttempt,
                        ProfileConvergenceAttempt.profile_convergence_job_id
                        == ProfileConvergenceJob.id,
                    )
                    .join(
                        RuntimeAgentProfile,
                        RuntimeAgentProfile.id
                        == ProfileConvergenceJob.runtime_agent_profile_id,
                    )
                    .where(
                        ProfileConvergenceJob.id == claimed.job_id,
                        ProfileConvergenceAttempt.id == claimed.attempt_id,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return
            job, attempt, profile = row
            if not self._owns(job, claimed):
                return
            revision = await db.scalar(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.runtime_agent_profile_id
                    == profile.id,
                    RuntimeAgentProfileRevision.revision
                    == claimed.target_revision,
                )
            )
            if revision is None:
                raise ProfileConvergenceError(
                    "PROFILE_REVISION_NOT_FOUND",
                    retryable=False,
                )
            profile.applied_revision = claimed.target_revision
            profile.applied_digest = str(revision.profile_digest)
            profile.effective_generation = int(response.effective_generation)
            profile.capability_health = [
                item.model_dump(mode="json") for item in response.capabilities
            ]
            attempt.status = "applied"
            attempt.stage = "complete"
            attempt.completed_at = now
            attempt.evidence = {
                "operation_id": claimed.request.operation_id,
                "digests": claimed.request.digests.model_dump(mode="json"),
                "effective_generation": response.effective_generation,
                "projection_status": response.status,
                "capabilities": profile.capability_health,
            }
            if int(job.target_revision) > claimed.target_revision:
                job.status = "waiting"
                job.stage = "resolve_desired"
                job.available_at = now
            else:
                job.status = "applied"
                job.stage = "complete"
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = None
            job.last_error_message = None
            await refresh_rollout_targets_for_profile(
                db,
                runtime_agent_profile_id=int(profile.id),
                applied_revision=claimed.target_revision,
                status="applied",
            )
            await db.commit()

    async def _fail(
        self,
        claimed: ClaimedProfileProjection,
        error: ProfileConvergenceError,
    ) -> None:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(ProfileConvergenceJob, ProfileConvergenceAttempt)
                    .join(
                        ProfileConvergenceAttempt,
                        ProfileConvergenceAttempt.profile_convergence_job_id
                        == ProfileConvergenceJob.id,
                    )
                    .where(
                        ProfileConvergenceJob.id == claimed.job_id,
                        ProfileConvergenceAttempt.id == claimed.attempt_id,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return
            job, attempt = row
            if not self._owns(job, claimed):
                return
            attempt.status = "waiting" if error.retryable else "failed"
            attempt.stage = "supervisor_apply"
            attempt.error_code = error.code
            attempt.error_message = error.code
            attempt.completed_at = now
            job.status = "waiting" if error.retryable else "failed"
            job.available_at = (
                now
                + timedelta(
                    seconds=min(60, 2 ** min(int(job.attempt_count or 1), 5))
                )
                if error.retryable
                else now
            )
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = error.code
            job.last_error_message = error.code
            profile = await db.get(
                RuntimeAgentProfile,
                int(job.runtime_agent_profile_id),
            )
            if profile is not None:
                await refresh_rollout_targets_for_profile(
                    db,
                    runtime_agent_profile_id=int(profile.id),
                    applied_revision=claimed.target_revision,
                    status="updating" if error.retryable else "failed",
                    error_code=error.code,
                )
            await db.commit()

    def _owns(
        self,
        job: ProfileConvergenceJob,
        claimed: ClaimedProfileProjection,
    ) -> bool:
        return (
            job.status == "updating"
            and job.lease_owner == self.worker_id
            and int(job.lease_epoch) == claimed.lease_epoch
        )


__all__ = [
    "ClaimedProfileProjection",
    "ProfileConvergenceError",
    "ProfileConvergenceWorker",
    "ProfileProjectionClient",
    "ProfileProjectionTargetResolver",
    "SqlAlchemyProfileProjectionTargetResolver",
]
