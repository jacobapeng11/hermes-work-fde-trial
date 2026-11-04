from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationAgentConversation,
    ApplicationAgentRun,
    ApplicationClient,
    User,
)
from app.services.runtime_workspace import Principal


_READ_ONLY_CHANNELS = frozenset({"wecom", "openapi"})


class SqlAlchemyApplicationAgentConversationViewService:
    """Build creator-facing, read-only views of channel/API conversations."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_read_only_sessions(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[dict]:
        channel_run_exists = (
            select(ApplicationAgentRun.id)
            .where(
                ApplicationAgentRun.org_id == principal.org_id,
                ApplicationAgentRun.application_agent_id == agent_id,
                ApplicationAgentRun.conversation_id
                == ApplicationAgentConversation.id,
                ApplicationAgentRun.channel.in_(_READ_ONLY_CHANNELS),
            )
            .exists()
        )
        rows = (
            await self.db.execute(
                select(
                    ApplicationAgentConversation,
                    User,
                    ApplicationClient,
                )
                .outerjoin(
                    User,
                    User.id
                    == ApplicationAgentConversation.subject_user_id,
                )
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentConversation.application_client_id,
                )
                .where(
                    ApplicationAgentConversation.org_id
                    == principal.org_id,
                    ApplicationAgentConversation.application_agent_id
                    == agent_id,
                    channel_run_exists,
                )
                .order_by(
                    ApplicationAgentConversation.last_active_at.desc(),
                    ApplicationAgentConversation.id.desc(),
                )
                .limit(50)
            )
        ).all()
        run_rows = (
            await self.db.execute(
                select(
                    ApplicationAgentRun.runtime_session_id,
                    ApplicationAgentRun.channel,
                ).where(
                    ApplicationAgentRun.org_id == principal.org_id,
                    ApplicationAgentRun.application_agent_id == agent_id,
                    ApplicationAgentRun.channel.in_(_READ_ONLY_CHANNELS),
                )
            )
        ).all()
        channels_by_session: dict[str, set[str]] = {}
        run_counts: dict[tuple[str, str], int] = {}
        for runtime_session_id, channel in run_rows:
            session_id = str(runtime_session_id)
            channel_name = str(channel)
            channels_by_session.setdefault(session_id, set()).add(channel_name)
            count_key = (session_id, channel_name)
            run_counts[count_key] = run_counts.get(count_key, 0) + 1

        records: list[dict] = []
        seen_session_ids: set[str] = set()
        for conversation, user, client in rows:
            session_id = str(conversation.runtime_session_id)
            if session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            channels = channels_by_session.get(session_id, set())
            channel = "openapi" if "openapi" in channels else "wecom"
            if channel == "openapi":
                title = _openapi_session_title(
                    conversation=conversation,
                    user=user,
                )
                origin_label = f"API · {str(client.name)}"
            else:
                title, origin_label = _wecom_debug_session_presentation(
                    conversation_key=str(conversation.conversation_key),
                    user_name=(
                        str(user.name)
                        if user is not None and user.name is not None
                        else None
                    ),
                    user_code=(
                        str(user.user_code)
                        if user is not None and user.user_code is not None
                        else None
                    ),
                )
            records.append(
                {
                    "runtime_session_id": session_id,
                    "title": title,
                    "message_count": run_counts.get(
                        (session_id, channel),
                        0,
                    ) * 2,
                    "source_type": channel,
                    "last_active_at": conversation.last_active_at,
                    "is_read_only": True,
                    "is_immutable": True,
                    "origin_label": origin_label,
                }
            )
        return records

    async def is_readable_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> bool:
        channel_run_exists = (
            select(ApplicationAgentRun.id)
            .where(
                ApplicationAgentRun.org_id == principal.org_id,
                ApplicationAgentRun.application_agent_id == agent_id,
                ApplicationAgentRun.conversation_id
                == ApplicationAgentConversation.id,
                ApplicationAgentRun.channel.in_(_READ_ONLY_CHANNELS),
            )
            .exists()
        )
        conversation_id = (
            await self.db.execute(
                select(ApplicationAgentConversation.id)
                .where(
                    ApplicationAgentConversation.org_id
                    == principal.org_id,
                    ApplicationAgentConversation.application_agent_id
                    == agent_id,
                    ApplicationAgentConversation.runtime_session_id
                    == runtime_session_id,
                    channel_run_exists,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return conversation_id is not None


def _openapi_session_title(
    *,
    conversation: ApplicationAgentConversation,
    user: User | None,
) -> str:
    subject_type = str(conversation.subject_type)
    if subject_type == "external_user":
        external_user_id = str(conversation.subject_service_id or "未知用户")
        return f"外部用户 · {external_user_id}"
    if subject_type == "user":
        name = str(user.name or "Enterprise SSO用户") if user is not None else "Enterprise SSO用户"
        code = str(user.user_code or "") if user is not None else ""
        identity = f"{name}（{code}）" if code else name
        return f"Enterprise SSO用户 · {identity}"
    return f"后台服务 · {str(conversation.conversation_key)}"


def _wecom_debug_session_presentation(
    *,
    conversation_key: str,
    user_name: str | None,
    user_code: str | None,
) -> tuple[str, str]:
    name = (user_name or "").strip() or "企业用户"
    code = (user_code or "").strip()
    identity = f"{name}（{code}）" if code else name
    if ":group:" in conversation_key:
        return f"{identity} · 群聊", "企微群聊"
    if ":direct:" in conversation_key:
        return f"{identity} · 单聊", "企微单聊"
    return f"{identity} · 企微会话", "企微"
