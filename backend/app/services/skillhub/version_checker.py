from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, settings
from app.models import Skill, UserSkillHubInstallation
from app.services.skillhub.client import SkillHubClient
from app.services.skillhub.market import SkillHubMarketService


class SkillHubVersionChecker:
    """Best-effort, per-user TTL refresh; reads never wait for the network."""

    _inflight: set[tuple[int, int]] = set()

    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], config: Settings = settings
    ) -> None:
        self.session_factory = session_factory
        self.config = config

    def refresh_stale(self, *, runtime_agent_profile_id: int, user_id: int) -> None:
        if not self.config.SKILLHUB_ENABLED:
            return
        key = (runtime_agent_profile_id, user_id)
        if key in self._inflight:
            return
        self._inflight.add(key)
        asyncio.create_task(self._run(key))

    async def _run(self, key: tuple[int, int]) -> None:
        profile_id, user_id = key
        try:
            async with self.session_factory() as session:
                cutoff = datetime.utcnow() - timedelta(
                    seconds=self.config.SKILLHUB_VERSION_CHECK_TTL_SECONDS
                )
                records = (
                    await session.execute(
                        select(Skill, UserSkillHubInstallation)
                        .join(
                            UserSkillHubInstallation,
                            UserSkillHubInstallation.skill_id == Skill.id,
                        )
                        .where(
                            UserSkillHubInstallation.runtime_agent_profile_id == profile_id,
                            UserSkillHubInstallation.user_id == user_id,
                            Skill.status.notin_(("deleted", "revoked")),
                        )
                    )
                ).all()
                client = SkillHubClient(config=self.config)
                market = SkillHubMarketService(
                    session=session, operation_queue=_NoopQueue(), client=client, config=self.config
                )
                for skill, record in records:
                    if (
                        record.last_version_checked_at is not None
                        and record.last_version_checked_at >= cutoff
                    ):
                        continue
                    try:
                        remote = await market.resolve_eligible(
                            namespace=record.namespace, public_slug=record.public_slug
                        )
                    except Exception as exc:
                        record.last_version_checked_at = datetime.utcnow()
                        code = str(getattr(exc, "code", "skillhub_check_failed"))
                        record.last_check_error = code[:1000]
                        if code in {
                            "skillhub_not_found",
                            "skillhub_not_eligible",
                            "skillhub_coordinate_mismatch",
                        }:
                            record.source_status = "source_removed"
                        continue
                    record.last_version_checked_at = datetime.utcnow()
                    record.last_check_error = None
                    if remote.skill.version != record.installed_upstream_version:
                        record.available_upstream_version = remote.skill.version
                        record.source_status = "update_available"
                    else:
                        record.available_upstream_version = None
                        record.source_status = "current"
                await session.commit()
        finally:
            self._inflight.discard(key)


class _NoopQueue:
    def pending(self, *_args, **_kwargs):
        return []
