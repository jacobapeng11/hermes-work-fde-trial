#!/usr/bin/env python3
"""Rotate one idle personal WeCom channel to a clean Runtime Session."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import AsyncSessionLocal
from app.models import (
    ImChannel,
    ImInboundMessage,
    RuntimeAgentProfile,
    RuntimeAuditLog,
    RuntimeRun,
    RuntimeSession,
)
from app.services.runtime_activity import ACTIVE_RUN_STATUSES
from app.services.runtime_sessions import generated_runtime_session_id


_UNFINISHED_INBOUND_STATUSES = (
    "received",
    "awaiting_identity",
    "staging_media",
    "queued",
    "running",
)


class PersonalWeComSessionRotationRejected(RuntimeError):
    """Raised when the selected channel cannot be rotated safely."""


@dataclass(frozen=True)
class PersonalWeComSessionRotation:
    channel_id: int
    old_runtime_session_row_id: int
    old_runtime_session_id: str
    new_runtime_session_row_id: int
    new_runtime_session_id: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def rotate_personal_wecom_session(
    db: AsyncSession,
    *,
    channel_id: int,
    expected_runtime_session_id: str,
    now: datetime | None = None,
) -> PersonalWeComSessionRotation:
    """Atomically rebind one idle personal WeCom channel to an empty session."""

    expected_runtime_session_id = expected_runtime_session_id.strip()
    if channel_id <= 0:
        raise ValueError("channel_id must be positive")
    if not expected_runtime_session_id:
        raise ValueError("expected_runtime_session_id is required")

    try:
        locked = (
            await db.execute(
                select(ImChannel, RuntimeSession)
                .join(
                    RuntimeSession,
                    RuntimeSession.id
                    == ImChannel.runtime_session_row_id,
                )
                .where(ImChannel.id == channel_id)
                .with_for_update()
            )
        ).first()
        if locked is None:
            raise PersonalWeComSessionRotationRejected(
                "personal WeCom channel or fixed Runtime Session was not found"
            )
        channel, old_session = locked
        if (
            channel.target_type != "personal_agent"
            or channel.provider != "wecom"
            or channel.runtime_session_row_id != old_session.id
            or channel.org_id != old_session.org_id
            or channel.user_id is None
            or channel.user_id != old_session.user_id
            or channel.runtime_workspace_id is None
            or channel.runtime_workspace_id
            != old_session.runtime_workspace_id
            or old_session.source_type != "wecom"
            or old_session.status != "active"
        ):
            raise PersonalWeComSessionRotationRejected(
                "personal WeCom channel/session invariant is broken"
            )

        profile = await db.scalar(
            select(RuntimeAgentProfile).where(
                RuntimeAgentProfile.id
                == old_session.runtime_agent_profile_id,
                RuntimeAgentProfile.org_id == old_session.org_id,
                RuntimeAgentProfile.user_id == old_session.user_id,
                RuntimeAgentProfile.runtime_workspace_id
                == old_session.runtime_workspace_id,
            )
        )
        if profile is None:
            raise PersonalWeComSessionRotationRejected(
                "personal WeCom Runtime Agent profile invariant is broken"
            )
        if (
            old_session.runtime_session_id
            != expected_runtime_session_id
        ):
            raise PersonalWeComSessionRotationRejected(
                "expected Runtime Session no longer matches the channel"
            )

        unfinished_inbound_id = await db.scalar(
            select(ImInboundMessage.id)
            .where(
                ImInboundMessage.channel_id == channel_id,
                ImInboundMessage.status.in_(
                    _UNFINISHED_INBOUND_STATUSES
                ),
            )
            .limit(1)
        )
        if unfinished_inbound_id is not None:
            raise PersonalWeComSessionRotationRejected(
                "personal WeCom channel has unfinished inbound work"
            )

        active_run_id = await db.scalar(
            select(RuntimeRun.id)
            .where(
                RuntimeRun.org_id == old_session.org_id,
                RuntimeRun.user_id == old_session.user_id,
                RuntimeRun.runtime_workspace_id
                == old_session.runtime_workspace_id,
                RuntimeRun.status.in_(
                    tuple(sorted(ACTIVE_RUN_STATUSES))
                ),
                or_(
                    RuntimeRun.session_id == old_session.id,
                    and_(
                        RuntimeRun.session_id.is_(None),
                        RuntimeRun.runtime_session_id
                        == old_session.runtime_session_id,
                    ),
                ),
            )
            .limit(1)
        )
        if active_run_id is not None:
            raise PersonalWeComSessionRotationRejected(
                "personal WeCom Runtime Session has an active run"
            )

        current = now or _now()
        new_session = RuntimeSession(
            org_id=int(old_session.org_id),
            user_id=int(old_session.user_id),
            runtime_workspace_id=int(
                old_session.runtime_workspace_id
            ),
            runtime_agent_profile_id=int(
                old_session.runtime_agent_profile_id
            ),
            runtime_session_id=generated_runtime_session_id(
                runtime_workspace_id=int(
                    old_session.runtime_workspace_id
                )
            ),
            source_type="wecom",
            title_cache="企微会话",
            message_count_cache=0,
            last_active_at=current,
            chat_log_backfilled_at=current,
            status="active",
            session_model_entry_id=old_session.session_model_entry_id,
        )
        db.add(new_session)
        await db.flush()
        if new_session.id is None:
            raise RuntimeError(
                "new personal WeCom Runtime Session was not persisted"
            )

        old_session_row_id = int(old_session.id)
        channel.runtime_session_row_id = int(new_session.id)
        db.add(
            RuntimeAuditLog(
                org_id=int(channel.org_id),
                actor_user_id=None,
                runtime_workspace_id=int(
                    old_session.runtime_workspace_id
                ),
                action="im.wecom.runtime_session_rotated",
                target_type="im_channel",
                target_id=str(channel.id),
                detail={
                    "provider": "wecom",
                    "old_runtime_session_row_id": old_session_row_id,
                    "old_runtime_session_id": str(
                        old_session.runtime_session_id
                    ),
                    "new_runtime_session_row_id": int(new_session.id),
                    "new_runtime_session_id": str(
                        new_session.runtime_session_id
                    ),
                },
            )
        )
        await db.commit()
        return PersonalWeComSessionRotation(
            channel_id=int(channel.id),
            old_runtime_session_row_id=old_session_row_id,
            old_runtime_session_id=str(
                old_session.runtime_session_id
            ),
            new_runtime_session_row_id=int(new_session.id),
            new_runtime_session_id=str(
                new_session.runtime_session_id
            ),
        )
    except Exception:
        await db.rollback()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically rotate one idle personal WeCom channel to a clean "
            "Runtime Session."
        )
    )
    parser.add_argument("--channel-id", type=int, required=True)
    parser.add_argument(
        "--expected-runtime-session-id",
        required=True,
    )
    return parser


async def run_cli(argv: Sequence[str]) -> PersonalWeComSessionRotation:
    args = build_parser().parse_args(list(argv))
    async with AsyncSessionLocal() as db:
        return await rotate_personal_wecom_session(
            db,
            channel_id=args.channel_id,
            expected_runtime_session_id=(
                args.expected_runtime_session_id
            ),
        )


def main(argv: Sequence[str] | None = None) -> int:
    result = asyncio.run(
        run_cli(sys.argv[1:] if argv is None else argv)
    )
    print(json.dumps(asdict(result), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
