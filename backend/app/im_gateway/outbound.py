from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.im_gateway.identity import opaque_digest
from app.im_gateway.scheduled_delivery import (
    personal_wecom_channel_is_scheduled_task_eligible,
)
from app.models import (
    ChatFileArtifact,
    ImChannel,
    ImChannelLease,
    ImOutboundDelivery,
    RuntimeRun,
    ScheduledTask,
)
from app.services.managed_secret import ManagedSecretCodec


_MAX_ATTEMPTS = 6
_PROCESSING_TIMEOUT = timedelta(minutes=5)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ClaimedImOutboundDelivery:
    delivery_id: int
    channel_id: int
    runtime_run_id: str
    org_id: int
    user_id: int
    runtime_workspace_id: int
    target: str
    target_digest: str
    content: str
    artifact_ids: tuple[int, ...]
    delivered_artifact_ids: tuple[int, ...]
    provider_delivered_text: str | None
    owner_instance_id: str
    lease_epoch: int


@dataclass(frozen=True)
class OutboundArtifact:
    artifact_id: int
    relative_path: str
    mime_type: str | None


class ImOutboundDeliveryStore:
    def __init__(
        self,
        db: AsyncSession,
        *,
        secret_codec: ManagedSecretCodec | None = None,
    ) -> None:
        self.db = db
        self.secret_codec = secret_codec or ManagedSecretCodec()

    async def claim_next(
        self,
        *,
        channel_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        now: datetime | None = None,
    ) -> ClaimedImOutboundDelivery | None:
        current = now or utc_now()
        lease = await self.db.scalar(
            select(ImChannelLease).where(
                ImChannelLease.channel_id == channel_id,
                ImChannelLease.owner_instance_id == owner_instance_id,
                ImChannelLease.lease_epoch == lease_epoch,
                ImChannelLease.expires_at > current,
            )
        )
        if lease is None:
            return None
        row = await self.db.scalar(
            select(ImOutboundDelivery)
            .where(
                ImOutboundDelivery.channel_id == channel_id,
                or_(
                    ImOutboundDelivery.status == "pending",
                    (
                        (ImOutboundDelivery.status == "retry_wait")
                        & or_(
                            ImOutboundDelivery.next_attempt_at.is_(None),
                            ImOutboundDelivery.next_attempt_at <= current,
                        )
                    ),
                    (
                        (ImOutboundDelivery.status == "delivering")
                        & (
                            ImOutboundDelivery.updated_at
                            <= current - _PROCESSING_TIMEOUT
                        )
                    ),
                ),
            )
            .order_by(ImOutboundDelivery.id)
            .limit(1)
            .with_for_update()
        )
        if row is None:
            return None
        channel = await self.db.scalar(
            select(ImChannel).where(ImChannel.id == channel_id)
        )
        if (
            not personal_wecom_channel_is_scheduled_task_eligible(channel)
            or channel is None
            or opaque_digest(str(channel.bot_id)) != row.bot_id_digest
            or channel.proactive_target_digest != row.target_digest
        ):
            await self._fail_locked(
                row,
                error_code="WECOM_DELIVERY_TARGET_CHANGED",
                error_message="WeCom proactive target changed before delivery",
            )
            await self.db.commit()
            return None
        run = await self.db.get(RuntimeRun, int(row.runtime_run_row_id))
        if run is None:
            await self._fail_locked(
                row,
                error_code="WECOM_DELIVERY_RUN_MISSING",
                error_message="Runtime Run is unavailable",
            )
            await self.db.commit()
            return None
        row.status = "delivering"
        row.processing_owner = owner_instance_id
        row.processing_lease_epoch = lease_epoch
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.next_attempt_at = None
        await self.db.commit()
        return ClaimedImOutboundDelivery(
            delivery_id=int(row.id),
            channel_id=channel_id,
            runtime_run_id=str(row.runtime_run_id),
            org_id=int(run.org_id),
            user_id=int(run.user_id),
            runtime_workspace_id=int(run.runtime_workspace_id),
            target=self.secret_codec.decrypt(str(row.target_encrypted)),
            target_digest=str(row.target_digest),
            content=str(row.content or ""),
            artifact_ids=tuple(int(value) for value in (row.artifact_ids or [])),
            delivered_artifact_ids=tuple(
                int(value) for value in (row.delivered_artifact_ids or [])
            ),
            provider_delivered_text=row.provider_delivered_text,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
        )

    async def artifacts(
        self, claimed: ClaimedImOutboundDelivery
    ) -> tuple[OutboundArtifact, ...]:
        pending_ids = set(claimed.artifact_ids) - set(
            claimed.delivered_artifact_ids
        )
        if not pending_ids:
            return ()
        rows = list(
            (
                await self.db.execute(
                    select(ChatFileArtifact).where(
                        ChatFileArtifact.id.in_(pending_ids),
                        ChatFileArtifact.org_id == claimed.org_id,
                        ChatFileArtifact.user_id == claimed.user_id,
                        ChatFileArtifact.runtime_workspace_id
                        == claimed.runtime_workspace_id,
                        ChatFileArtifact.status == "active",
                    )
                )
            ).scalars()
        )
        if {int(row.id) for row in rows} != pending_ids:
            raise RuntimeError(
                "outbound Runtime artifact is unavailable"
            )
        return tuple(
            OutboundArtifact(
                artifact_id=int(row.id),
                relative_path=str(row.relative_path),
                mime_type=row.mime_type,
            )
            for row in rows
        )

    async def mark_text_delivered(
        self,
        claimed: ClaimedImOutboundDelivery,
        *,
        delivered_text: str | None = None,
    ) -> None:
        row = await self._owned_row(claimed)
        row.provider_delivered_text = (
            claimed.content if delivered_text is None else delivered_text
        )
        await self.db.commit()

    async def mark_artifact_delivered(
        self,
        claimed: ClaimedImOutboundDelivery,
        *,
        artifact_id: int,
    ) -> None:
        row = await self._owned_row(claimed)
        delivered = {int(value) for value in (row.delivered_artifact_ids or [])}
        delivered.add(artifact_id)
        row.delivered_artifact_ids = sorted(delivered)
        await self.db.commit()

    async def mark_delivered(
        self, claimed: ClaimedImOutboundDelivery
    ) -> None:
        row = await self._owned_row(claimed)
        row.status = "delivered"
        row.delivered_at = utc_now()
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = None
        row.error_message = None
        await self.db.commit()

    async def mark_retry(
        self,
        claimed: ClaimedImOutboundDelivery,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        row = await self._owned_row(claimed)
        if int(row.attempt_count or 0) >= _MAX_ATTEMPTS:
            await self._fail_locked(
                row,
                error_code=error_code,
                error_message=error_message,
            )
        else:
            delay = min(300, 2 ** min(8, int(row.attempt_count or 1)))
            row.status = "retry_wait"
            row.next_attempt_at = utc_now() + timedelta(seconds=delay)
            row.processing_owner = None
            row.processing_lease_epoch = None
            row.error_code = error_code[:100]
            row.error_message = error_message[:512]
        await self.db.commit()

    async def _owned_row(
        self, claimed: ClaimedImOutboundDelivery
    ) -> ImOutboundDelivery:
        lease = await self.db.scalar(
            select(ImChannelLease).where(
                ImChannelLease.channel_id == claimed.channel_id,
                ImChannelLease.owner_instance_id
                == claimed.owner_instance_id,
                ImChannelLease.lease_epoch == claimed.lease_epoch,
                ImChannelLease.expires_at > utc_now(),
            )
        )
        if lease is None:
            raise RuntimeError("outbound channel lease was lost")
        row = await self.db.scalar(
            select(ImOutboundDelivery)
            .where(
                ImOutboundDelivery.id == claimed.delivery_id,
                ImOutboundDelivery.status == "delivering",
                ImOutboundDelivery.processing_owner
                == claimed.owner_instance_id,
                ImOutboundDelivery.processing_lease_epoch
                == claimed.lease_epoch,
            )
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("outbound delivery ownership was lost")
        return row

    async def _fail_locked(
        self,
        row: ImOutboundDelivery,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        row.status = "failed"
        row.next_attempt_at = None
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = error_code[:100]
        row.error_message = error_message[:512]
        task = await self.db.get(ScheduledTask, int(row.scheduled_task_id))
        if task is not None:
            task.last_run_status = "failed"
            task.last_error_code = "WECOM_DELIVERY_FAILED"
            task.last_error = "企微定时任务结果投递失败"
