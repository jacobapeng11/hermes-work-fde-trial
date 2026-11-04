from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    ProfileExecutionControlJob,
    RuntimeAgentProfile,
)
from app.schemas.asset_projection import (
    ProfileExecutionControlRequest,
    ProfileExecutionControlResponse,
)
from app.services.asset_rollout import refresh_rollout_batch
from app.services.profile_convergence import (
    ProfileProjectionTargetResolver,
    SqlAlchemyProfileProjectionTargetResolver,
)


logger = logging.getLogger(__name__)


class ProfileExecutionControlClient(Protocol):
    async def control_profile_execution(
        self,
        endpoint: RuntimeEndpoint,
        request: ProfileExecutionControlRequest,
    ) -> ProfileExecutionControlResponse: ...


class ProfileExecutionControlError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ClaimedProfileExecutionControl:
    job_id: int
    lease_epoch: int
    target_generation: int
    source_rollout_id: str
    request: ProfileExecutionControlRequest
    endpoint: RuntimeEndpoint


class ProfileExecutionControlService:
    """Persist the latest admission target without touching Profile Desired."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def queue(
        self,
        *,
        profile: RuntimeAgentProfile,
        action: str,
        source_rollout_id: str,
        reason_code: str,
        cancel_active_runs: bool = False,
    ) -> ProfileExecutionControlJob:
        if action not in {"revoke", "enable"}:
            raise ProfileExecutionControlError(
                "PROFILE_EXECUTION_ACTION_INVALID",
                retryable=False,
            )
        job = await self.db.scalar(
            select(ProfileExecutionControlJob)
            .where(
                ProfileExecutionControlJob.runtime_agent_profile_id == profile.id
            )
            .with_for_update()
        )
        generation = int(job.target_generation if job is not None else 0) + 1
        operation_id = f"{source_rollout_id}:{int(profile.id)}"
        now = datetime.utcnow()
        if job is None:
            job = ProfileExecutionControlJob(
                org_id=int(profile.org_id),
                runtime_workspace_id=int(profile.runtime_workspace_id),
                runtime_agent_profile_id=int(profile.id),
                source_rollout_id=source_rollout_id,
                operation_id=operation_id,
                target_action=action,
                target_generation=generation,
                reason_code=reason_code,
                cancel_active_runs=cancel_active_runs,
                status="waiting",
                available_at=now,
            )
            self.db.add(job)
        else:
            job.source_rollout_id = source_rollout_id
            job.operation_id = operation_id
            job.target_action = action
            job.target_generation = generation
            job.reason_code = reason_code
            job.cancel_active_runs = cancel_active_runs
            job.status = "waiting"
            job.available_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = None
            job.last_error_message = None
        await self.db.flush()
        return job


class ProfileExecutionControlWorker:
    """Deliver durable revoke/enable targets to the authoritative Supervisor."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        worker_id: str,
        client: ProfileExecutionControlClient | None = None,
        target_resolver: ProfileProjectionTargetResolver | None = None,
        lease_seconds: int = 60,
        concurrency: int = 4,
        batch_size: int = 16,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.client = client or HermesDashboardProtocolAdapter()
        self.target_resolver = (
            target_resolver or SqlAlchemyProfileProjectionTargetResolver()
        )
        self.lease_seconds = lease_seconds
        self.concurrency = concurrency
        self.batch_size = batch_size

    async def reconcile_once(self) -> int:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            ids = list(
                await db.scalars(
                    select(ProfileExecutionControlJob.id)
                    .where(
                        ProfileExecutionControlJob.available_at <= now,
                        or_(
                            ProfileExecutionControlJob.status == "waiting",
                            (
                                (ProfileExecutionControlJob.status == "updating")
                                & (
                                    ProfileExecutionControlJob.lease_expires_at.is_(
                                        None
                                    )
                                    | (
                                        ProfileExecutionControlJob.lease_expires_at
                                        <= now
                                    )
                                )
                            ),
                        ),
                    )
                    .order_by(
                        ProfileExecutionControlJob.available_at.asc(),
                        ProfileExecutionControlJob.id.asc(),
                    )
                    .limit(self.batch_size)
                )
            )
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(job_id: int) -> bool:
            async with semaphore:
                return await self._process(job_id)

        outcomes = await asyncio.gather(
            *(process(int(job_id)) for job_id in ids),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning(
                    "profile_execution_control_worker_error",
                    extra={"error_type": type(outcome).__name__},
                )
        return sum(outcome is True for outcome in outcomes)

    async def _process(self, job_id: int) -> bool:
        claimed = await self._claim(job_id)
        if claimed is None:
            return False
        try:
            response = await self.client.control_profile_execution(
                claimed.endpoint,
                claimed.request,
            )
            self._validate_response(claimed, response)
            if response.status == "failed":
                raise ProfileExecutionControlError(
                    response.error_code or "PROFILE_EXECUTION_CONTROL_FAILED",
                    retryable=False,
                )
        except ProfileExecutionControlError as exc:
            await self._fail(claimed, exc)
            return True
        except Exception as exc:
            await self._fail(
                claimed,
                ProfileExecutionControlError(
                    type(exc).__name__.upper()[:128],
                    retryable=True,
                ),
            )
            return True
        await self._complete(claimed)
        return True

    async def _claim(
        self,
        job_id: int,
    ) -> ClaimedProfileExecutionControl | None:
        now = datetime.utcnow()
        async with self.session_factory() as db:
            job = await db.scalar(
                select(ProfileExecutionControlJob)
                .where(ProfileExecutionControlJob.id == job_id)
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
            profile = await db.get(RuntimeAgentProfile, int(job.runtime_agent_profile_id))
            if profile is None or str(profile.status) == "deleted":
                job.status = "failed"
                job.last_error_code = "PROFILE_NOT_FOUND"
                await self._finish_rollout_target(
                    db,
                    rollout_id=str(job.source_rollout_id),
                    profile_id=int(job.runtime_agent_profile_id),
                    status="failed",
                    error_code="PROFILE_NOT_FOUND",
                )
                await db.commit()
                return None
            try:
                binding_id, endpoint = await self.target_resolver.resolve(
                    db,
                    profile=profile,
                )
            except Exception as exc:
                job.status = "waiting"
                job.available_at = now + timedelta(seconds=2)
                job.last_error_code = getattr(exc, "code", "RUNTIME_NOT_READY")
                job.last_error_message = job.last_error_code
                await db.commit()
                return None
            job.status = "updating"
            job.lease_owner = self.worker_id
            job.lease_epoch = int(job.lease_epoch or 0) + 1
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            job.attempt_count = int(job.attempt_count or 0) + 1
            job.last_error_code = None
            job.last_error_message = None
            claimed = ClaimedProfileExecutionControl(
                job_id=int(job.id),
                lease_epoch=int(job.lease_epoch),
                target_generation=int(job.target_generation),
                source_rollout_id=str(job.source_rollout_id),
                endpoint=endpoint,
                request=ProfileExecutionControlRequest(
                    operation_id=str(job.operation_id),
                    action=str(job.target_action),
                    runtime_id=str(endpoint.runtime_id),
                    binding_id=int(binding_id),
                    runtime_workspace_id=int(profile.runtime_workspace_id),
                    runtime_agent_profile_id=int(profile.id),
                    admission_generation=int(job.target_generation),
                    reason_code=str(job.reason_code),
                    cancel_active_runs=bool(job.cancel_active_runs),
                ),
            )
            await db.commit()
            return claimed

    @staticmethod
    def _validate_response(
        claimed: ClaimedProfileExecutionControl,
        response: ProfileExecutionControlResponse,
    ) -> None:
        request = claimed.request
        if (
            int(response.runtime_agent_profile_id)
            != int(request.runtime_agent_profile_id)
            or str(response.action) != str(request.action)
            or int(response.admission_generation)
            != int(request.admission_generation)
        ):
            raise ProfileExecutionControlError(
                "PROFILE_EXECUTION_CONTROL_RESULT_MISMATCH",
                retryable=False,
            )

    async def _complete(self, claimed: ClaimedProfileExecutionControl) -> None:
        async with self.session_factory() as db:
            job = await db.scalar(
                select(ProfileExecutionControlJob)
                .where(ProfileExecutionControlJob.id == claimed.job_id)
                .with_for_update()
            )
            if job is None or not self._owns(job, claimed):
                return
            job.status = "applied"
            job.applied_action = str(job.target_action)
            job.applied_generation = int(job.target_generation)
            job.lease_owner = None
            job.lease_expires_at = None
            await self._finish_rollout_target(
                db,
                rollout_id=claimed.source_rollout_id,
                profile_id=int(claimed.request.runtime_agent_profile_id),
                status=("revoked" if claimed.request.action == "revoke" else "applied"),
            )
            await db.commit()

    async def _fail(
        self,
        claimed: ClaimedProfileExecutionControl,
        error: ProfileExecutionControlError,
    ) -> None:
        async with self.session_factory() as db:
            job = await db.scalar(
                select(ProfileExecutionControlJob)
                .where(ProfileExecutionControlJob.id == claimed.job_id)
                .with_for_update()
            )
            if job is None or not self._owns(job, claimed):
                return
            job.status = "waiting" if error.retryable else "failed"
            job.available_at = (
                datetime.utcnow()
                + timedelta(seconds=min(60, 2 ** min(int(job.attempt_count), 5)))
                if error.retryable
                else datetime.utcnow()
            )
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = error.code
            job.last_error_message = error.code
            if not error.retryable:
                await self._finish_rollout_target(
                    db,
                    rollout_id=claimed.source_rollout_id,
                    profile_id=int(claimed.request.runtime_agent_profile_id),
                    status="failed",
                    error_code=error.code,
                )
            await db.commit()

    @staticmethod
    async def _finish_rollout_target(
        db: AsyncSession,
        *,
        rollout_id: str,
        profile_id: int,
        status: str,
        error_code: str | None = None,
    ) -> None:
        row = (
            await db.execute(
                select(AssetRolloutTarget, AssetRolloutBatch)
                .join(
                    AssetRolloutBatch,
                    AssetRolloutBatch.id == AssetRolloutTarget.asset_rollout_batch_id,
                )
                .where(
                    AssetRolloutBatch.rollout_id == rollout_id,
                    AssetRolloutTarget.runtime_agent_profile_id == profile_id,
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            return
        target, batch = row
        target.status = status
        target.last_error_code = error_code
        target.last_error_message = error_code
        if status in {"applied", "revoked"}:
            target.applied_at = datetime.utcnow()
        await db.flush()
        await refresh_rollout_batch(db, batch_id=int(batch.id))

    def _owns(
        self,
        job: ProfileExecutionControlJob,
        claimed: ClaimedProfileExecutionControl,
    ) -> bool:
        return (
            str(job.status) == "updating"
            and str(job.lease_owner) == self.worker_id
            and int(job.lease_epoch) == claimed.lease_epoch
            and int(job.target_generation) == claimed.target_generation
            and str(job.source_rollout_id) == claimed.source_rollout_id
        )


__all__ = [
    "ClaimedProfileExecutionControl",
    "ProfileExecutionControlClient",
    "ProfileExecutionControlError",
    "ProfileExecutionControlService",
    "ProfileExecutionControlWorker",
]
