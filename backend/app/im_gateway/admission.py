from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImChannel, ImInboundMessage, RuntimeSession
from app.services.runtime_workspace import Principal


async def wecom_queue_blocks_web_run(
    db: AsyncSession,
    *,
    principal: Principal,
    runtime_workspace_id: int,
    runtime_session_id: str,
) -> bool:
    """Block Web insertion only while the fixed Session has pending IM work."""

    result = await db.scalar(
        select(ImInboundMessage.id)
        .join(
            ImChannel,
            ImChannel.id == ImInboundMessage.channel_id,
        )
        .join(
            RuntimeSession,
            RuntimeSession.id == ImChannel.runtime_session_row_id,
        )
        .where(
            RuntimeSession.org_id == principal.org_id,
            RuntimeSession.user_id == principal.user_id,
            RuntimeSession.runtime_workspace_id
            == runtime_workspace_id,
            RuntimeSession.runtime_session_id == runtime_session_id,
            RuntimeSession.source_type == "wecom",
            ImInboundMessage.status.in_(
                ("received", "staging_media", "queued", "running")
            ),
        )
        .limit(1)
    )
    return result is not None
