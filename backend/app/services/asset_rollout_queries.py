from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    RuntimeAgentProfile,
    User,
)
from app.schemas.asset_rollout import (
    AssetRolloutBatchSummary,
    AssetRolloutDetail,
    AssetRolloutProfileProgress,
    AssetRolloutUserProgress,
)


class AssetRolloutQueryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AssetRolloutQueryService:
    """Read durable rollout progress grouped by affected user and Profile."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_batches(
        self,
        *,
        org_id: int,
        offset: int = 0,
        limit: int = 50,
    ) -> list[AssetRolloutBatchSummary]:
        rows = list(
            await self.db.scalars(
                select(AssetRolloutBatch)
                .where(AssetRolloutBatch.org_id == org_id)
                .order_by(AssetRolloutBatch.created_at.desc(), AssetRolloutBatch.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        return [self._batch(item) for item in rows]

    async def detail(
        self,
        *,
        org_id: int,
        rollout_id: str,
        status: str | None = None,
        user_id: int | None = None,
        profile_kind: str | None = None,
        error_code: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> AssetRolloutDetail:
        batch = await self.db.scalar(
            select(AssetRolloutBatch).where(
                AssetRolloutBatch.org_id == org_id,
                AssetRolloutBatch.rollout_id == rollout_id,
            )
        )
        if batch is None:
            raise AssetRolloutQueryError("ROLLOUT_NOT_FOUND")

        clauses = [AssetRolloutTarget.asset_rollout_batch_id == batch.id]
        if status:
            clauses.append(AssetRolloutTarget.status == status)
        if user_id is not None:
            clauses.append(AssetRolloutTarget.user_id == user_id)
        if profile_kind:
            clauses.append(RuntimeAgentProfile.profile_kind == profile_kind)
        if error_code:
            clauses.append(AssetRolloutTarget.last_error_code == error_code)

        user_ids_statement = (
            select(AssetRolloutTarget.user_id)
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.id == AssetRolloutTarget.runtime_agent_profile_id,
            )
            .where(*clauses)
            .group_by(AssetRolloutTarget.user_id)
        )
        total_users = int(
            await self.db.scalar(
                select(func.count()).select_from(user_ids_statement.subquery())
            )
            or 0
        )
        page_user_ids = list(
            await self.db.scalars(
                user_ids_statement.order_by(AssetRolloutTarget.user_id).offset(offset).limit(limit)
            )
        )
        if not page_user_ids:
            return AssetRolloutDetail(
                batch=self._batch(batch),
                total_users=total_users,
                offset=offset,
                limit=limit,
                users=[],
            )

        rows = (
            await self.db.execute(
                select(AssetRolloutTarget, RuntimeAgentProfile, User)
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.id == AssetRolloutTarget.runtime_agent_profile_id,
                )
                .outerjoin(User, User.id == AssetRolloutTarget.user_id)
                .where(*clauses, AssetRolloutTarget.user_id.in_(page_user_ids))
                .order_by(AssetRolloutTarget.user_id, RuntimeAgentProfile.id)
            )
        ).all()
        grouped: dict[int, AssetRolloutUserProgress] = {}
        for target, profile, user in rows:
            current = grouped.get(int(target.user_id))
            if current is None:
                current = AssetRolloutUserProgress(
                    user_id=int(target.user_id),
                    user_code=str(user.user_code) if user and user.user_code else None,
                    user_name=str(user.name) if user else None,
                    status="success",
                    profiles=[],
                )
                grouped[int(target.user_id)] = current
            current.profiles.append(
                AssetRolloutProfileProgress(
                    target_id=int(target.id),
                    user_id=int(target.user_id),
                    runtime_workspace_id=int(target.runtime_workspace_id),
                    runtime_agent_profile_id=int(target.runtime_agent_profile_id),
                    profile_kind=str(profile.profile_kind),
                    profile_name=str(profile.display_name),
                    target_revision=int(target.target_revision),
                    applied_revision=int(profile.applied_revision or 0),
                    status=str(target.status),
                    attempt_count=int(target.attempt_count or 0),
                    error_code=target.last_error_code,
                    error_message=target.last_error_message,
                )
            )
        users = [grouped[int(item)] for item in page_user_ids if int(item) in grouped]
        for item in users:
            item.status = self._user_status([profile.status for profile in item.profiles])
        return AssetRolloutDetail(
            batch=self._batch(batch),
            total_users=total_users,
            offset=offset,
            limit=limit,
            users=users,
        )

    @staticmethod
    def _user_status(statuses: list[str]) -> str:
        failed = sum(item == "failed" for item in statuses)
        active = any(item in {"waiting", "updating"} for item in statuses)
        if failed == len(statuses):
            return "failed"
        if failed:
            return "partial_failed"
        if active:
            return "updating"
        return "success"

    @staticmethod
    def _batch(batch: AssetRolloutBatch) -> AssetRolloutBatchSummary:
        return AssetRolloutBatchSummary(
            rollout_id=str(batch.rollout_id),
            asset_type=str(batch.asset_type),
            asset_id=int(batch.asset_id),
            action=str(batch.action),
            source_version_id=batch.source_version_id,
            target_version_id=batch.target_version_id,
            affected_user_count=int(batch.affected_user_count),
            affected_profile_count=int(batch.affected_profile_count),
            waiting_count=int(batch.waiting_count),
            updating_count=int(batch.updating_count),
            applied_count=int(batch.applied_count),
            failed_count=int(batch.failed_count),
            superseded_count=int(batch.superseded_count),
            revoked_count=int(batch.revoked_count),
            status=str(batch.status),
            created_at=batch.created_at,
            started_at=batch.started_at,
            completed_at=batch.completed_at,
        )


__all__ = ["AssetRolloutQueryError", "AssetRolloutQueryService"]
