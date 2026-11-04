from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models import Skill, UserSkillHubInstallation
from app.services.plaza.user_skill_operations.operation_queue import UserSkillOperationQueue
from app.services.skillhub.client import SkillHubClient, SkillHubError, SkillHubSkill


@dataclass(frozen=True)
class EligibleSkillHubSkill:
    skill: SkillHubSkill
    category_key: str


class SkillHubMarketService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        operation_queue: UserSkillOperationQueue,
        client: SkillHubClient | None = None,
        config: Settings = settings,
    ) -> None:
        self.session = session
        self.operation_queue = operation_queue
        self.client = client or SkillHubClient(config=config)
        self.config = config

    async def list(
        self,
        *,
        org_id: int,
        user_id: int,
        workspace_id: int,
        runtime_agent_profile_id: int,
        feed: str,
        category_key: str | None,
        query: str | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[EligibleSkillHubSkill], str | None]:
        if not self.config.SKILLHUB_ENABLED:
            return [], None
        size = min(
            limit or self.config.SKILLHUB_DEFAULT_PAGE_SIZE, self.config.SKILLHUB_MAX_PAGE_SIZE
        )
        # A provider page is deliberately small.  Filter failure must not make
        # the market look empty, but unbounded upstream scans would make a
        # common page visit slow and bypass the intended curated candidate pool.
        # Consume at most two raw cursor pages and return the cursor after the
        # last consumed page so "load more" never repeats items.
        eligible: list[EligibleSkillHubSkill] = []
        seen: set[tuple[str, str, str]] = set()
        next_cursor = cursor
        for page_index in range(2):
            # SkillHub's current public endpoint does not emit a cursor. In
            # that safe, bounded fallback, request exactly two display pages
            # worth of candidates once so filtering can still fill the 12-card
            # surface. Cursor-capable providers keep the normal one-page read.
            raw_limit = size * 2 if page_index == 0 and cursor is None else size
            page = await self.client.list_skills(
                feed=feed,
                query=query,
                cursor=next_cursor,
                limit=raw_limit,
            )
            for skill in page.items:
                item = self.eligible(skill)
                identity = (skill.namespace, skill.public_slug, skill.version)
                if item is None or identity in seen:
                    continue
                seen.add(identity)
                if category_key and item.category_key != category_key:
                    continue
                eligible.append(item)
                if len(eligible) >= size:
                    return eligible, page.next_cursor
            if not page.next_cursor or page.next_cursor == next_cursor:
                return eligible, page.next_cursor
            next_cursor = page.next_cursor
        return eligible, next_cursor

    async def resolve_eligible(
        self, *, namespace: str, public_slug: str, expected_version: str | None = None
    ) -> EligibleSkillHubSkill:
        if not self.config.SKILLHUB_ENABLED:
            raise SkillHubError("skillhub_disabled")
        skill = await self.client.resolve(
            namespace=namespace, public_slug=public_slug, version=expected_version
        )
        if skill.namespace != namespace or skill.public_slug != public_slug:
            raise SkillHubError("skillhub_coordinate_mismatch")
        if expected_version is not None and skill.version != expected_version:
            raise SkillHubError("skillhub_version_changed")
        eligible = self.eligible(skill)
        if eligible is None:
            raise SkillHubError("skillhub_not_eligible")
        return eligible

    def eligible(self, skill: SkillHubSkill) -> EligibleSkillHubSkill | None:
        if skill.downloads is None or skill.downloads < self.config.SKILLHUB_MIN_DOWNLOADS:
            return None
        if skill.installs is None or skill.installs < self.config.SKILLHUB_MIN_INSTALLS:
            return None
        # Missing source metadata is not trusted as free/public. SkillHub may
        # change its response shape; the safe default is to hide it.
        if (skill.visibility or "").casefold() not in {"public", "community"}:
            return None
        if (skill.pricing or "").casefold() not in {"free", "community", "public"}:
            return None
        return EligibleSkillHubSkill(
            skill=skill,
            category_key=self.config.skillhub_category_mapping.get(skill.category or "", "other"),
        )

    async def installed_by_coordinate(
        self, *, runtime_agent_profile_id: int
    ) -> dict[tuple[str, str], tuple[Skill, UserSkillHubInstallation]]:
        rows = await self.session.execute(
            select(Skill, UserSkillHubInstallation)
            .join(UserSkillHubInstallation, UserSkillHubInstallation.skill_id == Skill.id)
            .where(
                UserSkillHubInstallation.runtime_agent_profile_id == runtime_agent_profile_id,
                Skill.status.notin_(("deleted", "revoked")),
            )
        )
        return {
            (record.namespace, record.public_slug): (skill, record) for skill, record in rows.all()
        }

    def operating(
        self,
        *,
        workspace_id: int,
        org_id: int,
        user_id: int,
        runtime_agent_profile_id: int,
        namespace: str,
        public_slug: str,
    ) -> bool:
        target = skillhub_target(namespace, public_slug)
        return any(
            item.target_key == target and item.status in {"queued", "running"}
            for item in self.operation_queue.pending(
                workspace_id,
                org_id=org_id,
                user_id=user_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )
        )


def skillhub_target(namespace: str, public_slug: str) -> str:
    return f"skillhub:{namespace}:{public_slug}"
