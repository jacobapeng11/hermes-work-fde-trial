from __future__ import annotations

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationAgentWeComUserAccess,
    ImChannel,
    ImInboundMessage,
    OrganizationWeComIdentity,
    User,
)
from app.schemas.im_gateway import (
    ApplicationWeComUserListResponse,
    ApplicationWeComUserSummary,
)
from app.services.managed_secret import ManagedSecretCodec


async def list_application_wecom_users(
    db: AsyncSession,
    *,
    agent_id: int,
    org_id: int,
    query: str | None,
    limit: int,
    secret_codec: ManagedSecretCodec,
) -> ApplicationWeComUserListResponse:
    legacy_rows = (await db.execute(
        select(
            User,
            OrganizationWeComIdentity.id,
            ApplicationAgentWeComUserAccess.first_seen_at,
            ApplicationAgentWeComUserAccess.last_seen_at,
            ApplicationAgentWeComUserAccess.last_chat_type,
            OrganizationWeComIdentity.status,
            OrganizationWeComIdentity.member_id_encrypted,
        )
        .join(OrganizationWeComIdentity, OrganizationWeComIdentity.platform_user_id == User.id)
        .join(ApplicationAgentWeComUserAccess, ApplicationAgentWeComUserAccess.organization_wecom_identity_id == OrganizationWeComIdentity.id)
        .where(
            User.org_id == org_id,
            OrganizationWeComIdentity.org_id == org_id,
            ApplicationAgentWeComUserAccess.application_agent_id == agent_id,
        )
    )).all()
    latest = ImInboundMessage.__table__.alias("latest_wecom_message")
    newer = ImInboundMessage.__table__.alias("newer_wecom_message")
    first_seen = (
        select(
            ImInboundMessage.sender_platform_user_id.label("platform_user_id"),
            func.min(ImInboundMessage.received_at).label("first_seen_at"),
        )
        .join(ImChannel, ImChannel.id == ImInboundMessage.channel_id)
        .where(
            ImInboundMessage.application_agent_id == agent_id,
            ImInboundMessage.target_type == "application_agent",
            ImInboundMessage.provider == "wecom",
            ImInboundMessage.identity_status == "resolved",
            ImInboundMessage.sender_platform_user_id.is_not(None),
            ImChannel.org_id == org_id,
            ImChannel.application_agent_id == agent_id,
        )
        .group_by(ImInboundMessage.sender_platform_user_id)
        .subquery("wecom_user_first_seen")
    )
    current_rows = (await db.execute(
        select(
            User,
            latest.c.sender_platform_user_id,
            latest.c.received_at,
            latest.c.chat_type,
            latest.c.sender_member_id_encrypted,
            first_seen.c.first_seen_at,
        )
        .join(first_seen, first_seen.c.platform_user_id == User.id)
        .join(latest, latest.c.sender_platform_user_id == User.id)
        .outerjoin(newer, and_(
            newer.c.application_agent_id == agent_id,
            newer.c.target_type == "application_agent",
            newer.c.provider == "wecom",
            newer.c.identity_status == "resolved",
            newer.c.sender_platform_user_id == latest.c.sender_platform_user_id,
            or_(newer.c.received_at > latest.c.received_at, and_(newer.c.received_at == latest.c.received_at, newer.c.id > latest.c.id)),
        ))
        .join(ImChannel, ImChannel.id == latest.c.channel_id)
        .where(
            latest.c.application_agent_id == agent_id,
            latest.c.target_type == "application_agent",
            latest.c.provider == "wecom",
            latest.c.identity_status == "resolved",
            latest.c.sender_platform_user_id.is_not(None),
            ImChannel.org_id == org_id,
            ImChannel.application_agent_id == agent_id,
            newer.c.id.is_(None),
            User.org_id == org_id,
        )
    )).all()
    merged = {}
    for user, identity_id, first, last, chat_type, status, encrypted in legacy_rows:
        merged[int(user.id)] = (user, int(identity_id), first, last, chat_type, status, encrypted)
    for user, _platform_user_id, last, chat_type, encrypted, first in current_rows:
        previous = merged.get(int(user.id))
        merged[int(user.id)] = (
            user,
            None,
            min(previous[2], first) if previous is not None else first,
            last,
            chat_type,
            "active",
            encrypted,
        )
    candidates = list(merged.values())
    normalized = str(query or "").strip()
    if normalized:
        escaped = normalized.replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        candidates = [row for row in candidates if (
            (row[0].name and pattern[1:-1].replace("\\%", "%").replace("\\_", "_").lower() in str(row[0].name).lower())
            or (row[0].user_code and pattern[1:-1].replace("\\%", "%").replace("\\_", "_").lower() in str(row[0].user_code).lower())
        )]
    candidates.sort(key=lambda row: (row[3], row[0].id), reverse=True)
    count = len(candidates)
    rows = candidates[:max(1, min(limit, 200))]
    result = [
        ApplicationWeComUserSummary(
            identity_id=int(identity_id) if identity_id is not None else None,
            platform_user_id=int(user.id),
            user_name=str(user.name),
            user_code=str(user.user_code) if user.user_code is not None else None,
            member_id_masked=_mask_member_id(secret_codec.decrypt(str(member_encrypted)))
            if member_encrypted else "未知",
            status=str(status),
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            last_chat_type=str(last_chat_type),
        )
        for user, identity_id, first_seen_at, last_seen_at, last_chat_type, status, member_encrypted in rows
    ]
    return ApplicationWeComUserListResponse(users=result, total=count)


def _mask_member_id(value: str) -> str:
    normalized = str(value or "")
    if len(normalized) <= 4:
        return "****"
    return f"{normalized[:2]}****{normalized[-2:]}"
