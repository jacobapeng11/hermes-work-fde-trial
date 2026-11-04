from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    InstalledExpertBinding,
    ProfileConvergenceJob,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
)
from app.schemas.asset_rollout import AssetRolloutCommand, AssetRolloutResult
from app.services.expert_rollout import ExpertRolloutError, ExpertRolloutService
from app.services.profile_desired import ProfileDesiredError
from app.services.profile_desired_mutations import ProfileDesiredMutationService


class AssetRolloutError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AssetRolloutService:
    """Freeze one admin action into durable per-Profile work targets."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, command: AssetRolloutCommand) -> AssetRolloutResult:
        existing = await self.db.scalar(
            select(AssetRolloutBatch).where(
                AssetRolloutBatch.rollout_id == command.rollout_id
            )
        )
        if existing is not None:
            self._validate_idempotent(existing, command)
            return self._result(existing, created=False)

        profiles = await self._affected_profiles(command)
        now = datetime.utcnow()
        batch = AssetRolloutBatch(
            rollout_id=command.rollout_id,
            org_id=command.org_id,
            asset_type=command.asset_type,
            asset_id=command.asset_id,
            action=command.action,
            source_version_id=command.source_version_id,
            target_version_id=command.target_version_id,
            created_by_user_id=command.created_by_user_id,
            change_summary=command.change_summary,
            affected_user_count=len({int(item.user_id) for item in profiles}),
            affected_profile_count=len(profiles),
            waiting_count=len(profiles),
            status="waiting" if profiles else "completed",
            snapshot_at=now,
            completed_at=now if not profiles else None,
        )
        self.db.add(batch)
        await self.db.flush()

        for profile in profiles:
            await self._supersede_older_target(command=command, profile=profile)
            self.db.add(
                AssetRolloutTarget(
                    asset_rollout_batch_id=int(batch.id),
                    org_id=command.org_id,
                    user_id=int(profile.user_id),
                    runtime_workspace_id=int(profile.runtime_workspace_id),
                    runtime_agent_profile_id=int(profile.id),
                    target_revision=int(profile.desired_revision or 0),
                    status="waiting",
                )
            )
        await self.db.flush()
        return self._result(batch, created=True)

    async def retry_failed(
        self,
        *,
        org_id: int,
        rollout_id: str,
        target_id: int | None = None,
    ) -> int:
        batch = await self.db.scalar(
            select(AssetRolloutBatch)
            .where(
                AssetRolloutBatch.org_id == org_id,
                AssetRolloutBatch.rollout_id == rollout_id,
            )
            .with_for_update()
        )
        if batch is None:
            raise AssetRolloutError("ROLLOUT_NOT_FOUND")
        statement = (
            select(AssetRolloutTarget)
            .where(
                AssetRolloutTarget.asset_rollout_batch_id == batch.id,
                AssetRolloutTarget.status == "failed",
            )
            .with_for_update()
        )
        if target_id is not None:
            statement = statement.where(AssetRolloutTarget.id == target_id)
        targets = list(await self.db.scalars(statement))
        for target in targets:
            target.status = "waiting"
            target.last_error_code = None
            target.last_error_message = None
            target.applied_at = None
            target.activated_at = None
        if targets:
            batch.completed_at = None
            await self.db.flush()
            await refresh_rollout_batch(self.db, batch_id=int(batch.id))
        return len(targets)

    async def _affected_profiles(
        self,
        command: AssetRolloutCommand,
    ) -> list[RuntimeAgentProfile]:
        statement = select(RuntimeAgentProfile).where(
            RuntimeAgentProfile.org_id == command.org_id,
            RuntimeAgentProfile.status != "deleted",
        )
        if command.asset_type == "skill":
            statement = statement.join(
                ProfileSkillBinding,
                ProfileSkillBinding.runtime_agent_profile_id
                == RuntimeAgentProfile.id,
            ).where(
                ProfileSkillBinding.skill_id == command.asset_id,
                ProfileSkillBinding.status == "enabled",
            )
        elif command.asset_type == "mcp":
            statement = statement.join(
                ProfileMcpBinding,
                ProfileMcpBinding.runtime_agent_profile_id
                == RuntimeAgentProfile.id,
            ).where(
                ProfileMcpBinding.mcp_id == command.asset_id,
                ProfileMcpBinding.status == "enabled",
            )
        elif command.action in {"revoke", "enable"}:
            statement = statement.where(
                RuntimeAgentProfile.expert_package_id == command.asset_id,
                RuntimeAgentProfile.profile_kind == "expert",
                RuntimeAgentProfile.status != "deleted",
            )
        else:
            statement = statement.join(
                InstalledExpertBinding,
                InstalledExpertBinding.runtime_agent_profile_id
                == RuntimeAgentProfile.id,
            ).where(
                InstalledExpertBinding.expert_package_id == command.asset_id,
                InstalledExpertBinding.status == "enabled",
            )
        return list(
            (
                await self.db.execute(
                    statement.order_by(RuntimeAgentProfile.id.asc())
                )
            )
            .scalars()
            .unique()
            .all()
        )

    async def _supersede_older_target(
        self,
        *,
        command: AssetRolloutCommand,
        profile: RuntimeAgentProfile,
    ) -> None:
        action_group = (
            ("revoke", "enable")
            if command.action in {"revoke", "enable"}
            else ("publish", "rollback", "delete")
        )
        rows = (
            (
                await self.db.execute(
                    select(AssetRolloutTarget, AssetRolloutBatch)
                    .join(
                        AssetRolloutBatch,
                        AssetRolloutBatch.id
                        == AssetRolloutTarget.asset_rollout_batch_id,
                    )
                    .where(
                        AssetRolloutBatch.org_id == command.org_id,
                        AssetRolloutBatch.asset_type == command.asset_type,
                        AssetRolloutBatch.asset_id == command.asset_id,
                        AssetRolloutBatch.action.in_(action_group),
                        AssetRolloutTarget.runtime_agent_profile_id == profile.id,
                        AssetRolloutTarget.status.in_(("waiting", "updating")),
                    )
                    .with_for_update()
                )
            )
            .all()
        )
        affected_batches: set[int] = set()
        for target, batch in rows:
            target.status = "superseded"
            target.last_error_code = "ROLLOUT_SUPERSEDED"
            target.last_error_message = "A newer rollout replaced this target"
            affected_batches.add(int(batch.id))
        for batch_id in affected_batches:
            await refresh_rollout_batch(self.db, batch_id=batch_id)

    @staticmethod
    def _validate_idempotent(
        existing: AssetRolloutBatch,
        command: AssetRolloutCommand,
    ) -> None:
        identity = (
            str(existing.asset_type),
            int(existing.asset_id),
            str(existing.action),
            existing.source_version_id,
            existing.target_version_id,
        )
        requested = (
            command.asset_type,
            command.asset_id,
            command.action,
            command.source_version_id,
            command.target_version_id,
        )
        if identity != requested:
            raise AssetRolloutError("ROLLOUT_IDEMPOTENCY_CONFLICT")

    @staticmethod
    def _result(batch: AssetRolloutBatch, *, created: bool) -> AssetRolloutResult:
        return AssetRolloutResult(
            rollout_id=str(batch.rollout_id),
            affected_user_count=int(batch.affected_user_count),
            affected_profile_count=int(batch.affected_profile_count),
            status=str(batch.status),
            created=created,
        )


class AssetRolloutWorker:
    """Convert durable rollout targets into the latest complete Profile Desired."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        concurrency: int = 4,
        batch_size: int = 16,
    ) -> None:
        self.session_factory = session_factory
        self.concurrency = concurrency
        self.batch_size = batch_size

    async def reconcile_once(self) -> int:
        async with self.session_factory() as db:
            ids = list(
                await db.scalars(
                    select(AssetRolloutTarget.id)
                    .where(AssetRolloutTarget.status == "waiting")
                    .order_by(AssetRolloutTarget.id.asc())
                    .limit(self.batch_size)
                )
            )
        semaphore = asyncio.Semaphore(self.concurrency)

        async def process(target_id: int) -> bool:
            async with semaphore:
                return await self._process(target_id)

        outcomes = await asyncio.gather(
            *(process(int(target_id)) for target_id in ids),
            return_exceptions=True,
        )
        return sum(outcome is True for outcome in outcomes)

    async def _process(self, target_id: int) -> bool:
        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(AssetRolloutTarget, AssetRolloutBatch, RuntimeAgentProfile)
                    .join(
                        AssetRolloutBatch,
                        AssetRolloutBatch.id
                        == AssetRolloutTarget.asset_rollout_batch_id,
                    )
                    .join(
                        RuntimeAgentProfile,
                        RuntimeAgentProfile.id
                        == AssetRolloutTarget.runtime_agent_profile_id,
                    )
                    .where(AssetRolloutTarget.id == target_id)
                    .with_for_update()
                )
            ).first()
            if row is None:
                return False
            target, batch, profile = row
            if target.status != "waiting":
                return False
            target.status = "updating"
            target.attempt_count = int(target.attempt_count or 0) + 1
            target.last_error_code = None
            target.last_error_message = None
            batch.status = "updating"
            batch.started_at = batch.started_at or datetime.utcnow()
            try:
                if (
                    str(batch.asset_type) == "expert"
                    and str(batch.action) in {"revoke", "enable"}
                ):
                    from app.services.profile_execution_control import (
                        ProfileExecutionControlService,
                    )

                    await ProfileExecutionControlService(db).queue(
                        profile=profile,
                        action=str(batch.action),
                        source_rollout_id=str(batch.rollout_id),
                        reason_code=(
                            "EXPERT_FORCE_DISABLED"
                            if str(batch.action) == "revoke"
                            else "EXPERT_FORCE_DISABLE_CLEARED"
                        ),
                    )
                    target.target_revision = int(profile.applied_revision or 0)
                    target.status = "updating"
                    result = None
                elif str(batch.asset_type) == "expert":
                    result = await ExpertRolloutService(db).apply(
                        profile=profile,
                        batch=batch,
                    )
                else:
                    result = await ProfileDesiredMutationService(
                        db
                    ).refresh_asset_for_rollout(
                        profile=profile,
                        asset_type=str(batch.asset_type),
                        asset_id=int(batch.asset_id),
                        action=str(batch.action),
                        rollout_id=str(batch.rollout_id),
                        created_by_user_id=int(batch.created_by_user_id),
                    )
                if result is not None:
                    target.target_revision = result.desired_revision
                    target.status = (
                        "applied"
                        if result.convergence_status == "applied"
                        else "updating"
                    )
                    job = await db.scalar(
                        select(ProfileConvergenceJob).where(
                            ProfileConvergenceJob.runtime_agent_profile_id == profile.id
                        )
                    )
                    target.convergence_job_id = int(job.id) if job is not None else None
                    if target.status == "applied":
                        target.applied_at = datetime.utcnow()
            except (AssetRolloutError, ExpertRolloutError, ProfileDesiredError) as exc:
                target.status = (
                    "superseded"
                    if getattr(exc, "code", None) == "ROLLOUT_SUPERSEDED"
                    else "failed"
                )
                target.last_error_code = getattr(exc, "code", type(exc).__name__)
                target.last_error_message = str(exc)[:1000]
            await refresh_rollout_batch(db, batch_id=int(batch.id))
            await db.commit()
            return True


async def refresh_rollout_targets_for_profile(
    db: AsyncSession,
    *,
    runtime_agent_profile_id: int,
    applied_revision: int,
    status: str,
    error_code: str | None = None,
) -> None:
    rows = list(
        (
            await db.execute(
                select(AssetRolloutTarget, AssetRolloutBatch)
                .join(
                    AssetRolloutBatch,
                    AssetRolloutBatch.id == AssetRolloutTarget.asset_rollout_batch_id,
                )
                .where(
                    AssetRolloutTarget.runtime_agent_profile_id
                    == runtime_agent_profile_id,
                    AssetRolloutTarget.target_revision <= applied_revision,
                    AssetRolloutTarget.status.in_(("waiting", "updating")),
                    or_(
                        AssetRolloutBatch.action.notin_(("revoke", "enable")),
                        AssetRolloutBatch.asset_type.in_(("skill", "mcp")),
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    batch_ids: set[int] = set()
    now = datetime.utcnow()
    for target, _batch in rows:
        target.status = status
        target.last_error_code = error_code
        target.last_error_message = error_code
        if status == "applied":
            target.applied_at = now
        batch_ids.add(int(target.asset_rollout_batch_id))
    for batch_id in batch_ids:
        await refresh_rollout_batch(db, batch_id=batch_id)


async def refresh_rollout_batch(db: AsyncSession, *, batch_id: int) -> None:
    batch = await db.get(AssetRolloutBatch, batch_id)
    if batch is None:
        return
    counts = dict(
        (
            await db.execute(
                select(AssetRolloutTarget.status, func.count(AssetRolloutTarget.id))
                .where(AssetRolloutTarget.asset_rollout_batch_id == batch_id)
                .group_by(AssetRolloutTarget.status)
            )
        ).all()
    )
    batch.waiting_count = int(counts.get("waiting", 0))
    batch.updating_count = int(counts.get("updating", 0))
    batch.applied_count = int(counts.get("applied", 0))
    batch.failed_count = int(counts.get("failed", 0))
    batch.superseded_count = int(counts.get("superseded", 0))
    batch.revoked_count = int(counts.get("revoked", 0))
    unfinished = batch.waiting_count + batch.updating_count
    if unfinished:
        batch.status = "updating" if batch.updating_count else "waiting"
        return
    if batch.failed_count == 0:
        batch.status = "completed"
    elif batch.applied_count or batch.superseded_count or batch.revoked_count:
        batch.status = "partial_failed"
    else:
        batch.status = "failed"
    batch.completed_at = batch.completed_at or datetime.utcnow()


__all__ = [
    "AssetRolloutError",
    "AssetRolloutService",
    "AssetRolloutWorker",
    "refresh_rollout_batch",
    "refresh_rollout_targets_for_profile",
]
