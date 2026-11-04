from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.functions import coalesce

from app.models import (
    ChatFileArtifact,
    ChatLog,
    CloudHermesProfile,
    ImChannel,
    RuntimeAgentProfile,
    RuntimeAuditLog,
    RuntimeRun,
    RuntimeRunEvent,
    RuntimeSession,
)
from app.im_gateway.scheduled_delivery import (
    personal_wecom_channel_is_scheduled_task_eligible,
)
from app.repositories.scheduled_task import ScheduledTaskRepository
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_workspace import Principal


EXPERT_DELETED_SESSION_STATUS = "expert_deleted"
CONTEXT_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "canceled", "cancel_failed"}
)


class RuntimeSessionDeletedError(PermissionError):
    code = "expert_session_deleted"


class RuntimeSessionImmutableError(PermissionError):
    code = "wecom_session_fixed"


class RuntimeSessionProfilePreparingError(RuntimeError):
    """The owned Profile exists, but its first executable revision is not applied."""

    code = "runtime_agent_profile_preparing"


@dataclass(frozen=True)
class RuntimeSessionLatestRunRecord:
    runtime_run_id: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class RuntimeSessionRecord:
    session_id: int
    runtime_workspace_id: int
    org_id: int
    user_id: int
    runtime_session_id: str
    title: str | None
    source_type: str
    message_count: int
    session_model_entry_id: int | None
    status: str
    last_active_at: datetime | None = None
    hermes_materialized_at: datetime | None = None
    runtime_agent_profile_id: int | None = None
    agent_display_name: str | None = None
    agent_avatar_url: str | None = None
    agent_profile_kind: str | None = None
    hermes_profile_name: str | None = None
    context_source_runtime_session_id: str | None = None
    context_head_runtime_session_id: str | None = None
    scheduled_task_eligible: bool = True
    latest_run: RuntimeSessionLatestRunRecord | None = None

    @property
    def is_read_only(self) -> bool:
        return self.status == EXPERT_DELETED_SESSION_STATUS

    @property
    def is_immutable(self) -> bool:
        return self.source_type == "wecom"

    @property
    def origin_label(self) -> str | None:
        return "企微" if self.source_type == "wecom" else None

    @property
    def unavailable_reason(self) -> str | None:
        return "expert_deleted" if self.is_read_only else None


@dataclass(frozen=True)
class CreateRuntimeSessionCommand:
    runtime_workspace_id: int
    title: str | None = None
    model_entry_id: int | None = None
    source_type: str = "web"
    runtime_agent_profile_id: int | None = None
    context_source_runtime_session_id: str | None = None


class RuntimeSessionActiveRunError(RuntimeError):
    pass


class RuntimeSessionTitleConflictError(RuntimeError):
    pass


class RuntimeSessionContextBranchError(RuntimeError):
    pass


class RuntimeSessionStore(Protocol):
    async def list_sessions(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> list[RuntimeSessionRecord]:
        ...

    async def create_session(
        self,
        *,
        org_id: int,
        user_id: int,
        command: CreateRuntimeSessionCommand,
    ) -> RuntimeSessionRecord:
        ...

    async def get_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSessionRecord:
        ...

    async def rename_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> RuntimeSessionRecord:
        ...

    async def assert_title_available(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> None:
        ...

    async def delete_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        ...

    async def mark_session_deleting(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        ...

    async def mark_hermes_materialized(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        ...


class RuntimeSessionService:
    def __init__(self, store: RuntimeSessionStore) -> None:
        self.store = store

    async def list_sessions(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> list[RuntimeSessionRecord]:
        return await self.store.list_sessions(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
        )

    async def create_session(
        self,
        *,
        principal: Principal,
        command: CreateRuntimeSessionCommand,
    ) -> RuntimeSessionRecord:
        if command.source_type == "wecom":
            raise RuntimeSessionImmutableError(
                "wecom sessions are server-managed"
            )
        return await self.store.create_session(
            org_id=principal.org_id,
            user_id=principal.user_id,
            command=command,
        )

    async def get_session(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSessionRecord:
        return await self.store.get_session(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )

    async def rename_session(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> RuntimeSessionRecord:
        return await self.store.rename_session(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            title=title,
        )

    async def assert_title_available(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> None:
        await self.store.assert_title_available(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            title=title,
        )

    async def delete_session(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        await self.store.delete_session(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )

    async def mark_session_deleting(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        await self.store.mark_session_deleting(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )

    async def mark_hermes_materialized(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        await self.store.mark_hermes_materialized(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )


def default_runtime_session_id(*, runtime_workspace_id: int, session_id: int) -> str:
    return f"rs-{runtime_workspace_id}-{session_id}"


def generated_runtime_session_id(*, runtime_workspace_id: int) -> str:
    return f"rs-{runtime_workspace_id}-{uuid4().hex[:12]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SqlAlchemyRuntimeSessionStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_sessions(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> list[RuntimeSessionRecord]:
        latest_run_ids = (
            select(
                RuntimeRun.session_id.label("session_id"),
                func.max(RuntimeRun.id).label("runtime_run_row_id"),
            )
            .where(
                RuntimeRun.org_id == org_id,
                RuntimeRun.user_id == user_id,
                RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                RuntimeRun.session_id.is_not(None),
            )
            .group_by(RuntimeRun.session_id)
            .subquery()
        )
        context_head = aliased(RuntimeSession)
        base_result = await self.db.execute(
            select(RuntimeSession, RuntimeAgentProfile, ImChannel, RuntimeRun)
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.id == RuntimeSession.runtime_agent_profile_id,
            )
            .outerjoin(
                ImChannel,
                ImChannel.runtime_session_row_id == RuntimeSession.id,
            )
            .outerjoin(
                latest_run_ids,
                latest_run_ids.c.session_id == RuntimeSession.id,
            )
            .outerjoin(
                RuntimeRun,
                RuntimeRun.id == latest_run_ids.c.runtime_run_row_id,
            )
            .outerjoin(
                context_head,
                and_(
                    context_head.org_id == org_id,
                    context_head.user_id == user_id,
                    context_head.runtime_workspace_id == runtime_workspace_id,
                    context_head.status == "active",
                    context_head.context_source_runtime_session_id.is_not(None),
                    context_head.runtime_session_id
                    == RuntimeSession.context_head_runtime_session_id,
                ),
            )
            .where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.status.in_(("active", EXPERT_DELETED_SESSION_STATUS)),
                or_(
                    RuntimeSession.context_source_runtime_session_id.is_(None),
                    RuntimeSession.status == EXPERT_DELETED_SESSION_STATUS,
                ),
            )
            .order_by(
                coalesce(
                    context_head.last_active_at,
                    RuntimeSession.last_active_at,
                ).desc(),
                RuntimeSession.id.desc(),
            )
            .limit(50)
        )

        base_rows = base_result.all()

        def records_from_rows(rows) -> list[RuntimeSessionRecord]:
            return [
                self._record_from_model(
                    session,
                    agent_profile,
                    scheduled_task_eligible=(
                        personal_wecom_channel_is_scheduled_task_eligible(channel)
                        if session.source_type == "wecom"
                        else True
                    ),
                    latest_run=latest_run,
                )
                for session, agent_profile, channel, latest_run in rows
            ]

        records = records_from_rows(base_rows)
        context_head_ids = {
            session.context_head_runtime_session_id
            for session, _profile, _channel, _latest_run in base_rows
            if session.context_head_runtime_session_id is not None
        }
        if not context_head_ids:
            return records

        head_result = await self.db.execute(
            select(RuntimeSession, RuntimeAgentProfile, ImChannel, RuntimeRun)
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.id == RuntimeSession.runtime_agent_profile_id,
            )
            .outerjoin(
                ImChannel,
                ImChannel.runtime_session_row_id == RuntimeSession.id,
            )
            .outerjoin(
                latest_run_ids,
                latest_run_ids.c.session_id == RuntimeSession.id,
            )
            .outerjoin(
                RuntimeRun,
                RuntimeRun.id == latest_run_ids.c.runtime_run_row_id,
            )
            .where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.status == "active",
                RuntimeSession.context_source_runtime_session_id.is_not(None),
                RuntimeSession.runtime_session_id.in_(context_head_ids),
            )
            .order_by(RuntimeSession.last_active_at.desc(), RuntimeSession.id.desc())
        )
        records.extend(records_from_rows(head_result.all()))
        return records

    async def create_session(
        self,
        *,
        org_id: int,
        user_id: int,
        command: CreateRuntimeSessionCommand,
    ) -> RuntimeSessionRecord:
        await self._assert_workspace_owner(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=command.runtime_workspace_id,
        )
        agent_profile = await self._resolve_agent_profile(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_agent_profile_id=command.runtime_agent_profile_id,
        )
        source_session = None
        context_root = None
        source_logs: list[ChatLog] = []
        if command.context_source_runtime_session_id is not None:
            source_session = await self._owned_active_session(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=command.runtime_workspace_id,
                runtime_session_id=command.context_source_runtime_session_id,
            )
            if int(agent_profile.id) == int(source_session.runtime_agent_profile_id):
                raise RuntimeSessionContextBranchError(
                    "context branch must switch to a different agent profile"
                )
            if source_session.source_type in {"wecom", "openapi"}:
                raise RuntimeSessionContextBranchError(
                    "context branch source must be an interactive session"
                )
            context_root = await self._context_root_session(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=command.runtime_workspace_id,
                session=source_session,
            )
            active_run_id = await self.db.scalar(
                select(RuntimeRun.id)
                .where(
                    RuntimeRun.session_id == source_session.id,
                    RuntimeRun.status.in_(("pending", "accepted", "running")),
                )
                .limit(1)
            )
            if active_run_id is not None:
                raise RuntimeSessionActiveRunError()
            unprojected_terminal_run_id = await self.db.scalar(
                select(RuntimeRun.id)
                .where(
                    RuntimeRun.session_id == source_session.id,
                    RuntimeRun.status.in_(CONTEXT_TERMINAL_RUN_STATUSES),
                    RuntimeRun.chat_log_projected_at.is_(None),
                )
                .with_for_update(read=True)
                .limit(1)
            )
            if unprojected_terminal_run_id is not None:
                raise RuntimeSessionContextBranchError(
                    "context history projection is incomplete"
                )
            source_logs = list(
                (
                    await self.db.execute(
                        select(ChatLog)
                        .where(ChatLog.session_id == source_session.id)
                        .order_by(ChatLog.message_seq.asc())
                        .with_for_update(read=True)
                    )
                )
                .scalars()
            )
            if len(source_logs) < int(source_session.message_count_cache or 0):
                raise RuntimeSessionContextBranchError(
                    "context history projection is incomplete"
                )
            if len(source_logs) > 10_000:
                raise RuntimeSessionContextBranchError(
                    "context history exceeds the import limit"
                )
            history_bytes = sum(
                len(str(item.role).encode("utf-8"))
                + len(str(item.content).encode("utf-8"))
                for item in source_logs
            )
            if history_bytes > 4 * 1024 * 1024:
                raise RuntimeSessionContextBranchError(
                    "context history exceeds the import size limit"
                )
        now = utc_now()
        session = RuntimeSession(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_agent_profile_id=int(agent_profile.id),
            runtime_session_id=generated_runtime_session_id(runtime_workspace_id=command.runtime_workspace_id),
            context_source_runtime_session_id=(
                source_session.runtime_session_id if source_session is not None else None
            ),
            source_type=command.source_type,
            title_cache=command.title or (source_session.title_cache if source_session else None),
            message_count_cache=len(source_logs),
            last_active_at=now,
            chat_log_backfilled_at=now,
            status="active",
            session_model_entry_id=command.model_entry_id,
        )
        self.db.add(session)
        if source_session is not None:
            await self.db.flush()
            for source_log in source_logs:
                self.db.add(
                    ChatLog(
                        org_id=source_log.org_id,
                        user_id=source_log.user_id,
                        runtime_workspace_id=source_log.runtime_workspace_id,
                        session_id=session.id,
                        runtime_session_id=session.runtime_session_id,
                        runtime_run_id=None,
                        message_seq=source_log.message_seq,
                        role=source_log.role,
                        content=source_log.content,
                        content_format=source_log.content_format,
                        message_status=source_log.message_status,
                        channel=source_log.channel,
                        channel_account_id=source_log.channel_account_id,
                        external_conversation_id=None,
                        external_message_id=None,
                        source_type=source_log.source_type,
                        source_detail=source_log.source_detail,
                        context_runtime_agent_profile_id=(
                            source_log.context_runtime_agent_profile_id
                            if source_log.context_runtime_agent_profile_id is not None
                            else int(source_session.runtime_agent_profile_id)
                        ),
                        model_entry_id=source_log.model_entry_id,
                        ingest_source="hermes_backfill",
                        occurred_at=source_log.occurred_at,
                        completed_at=source_log.completed_at,
                    )
                )
        if context_root is not None:
            context_root.context_head_runtime_session_id = session.runtime_session_id
            context_root.last_active_at = now
        await self.db.commit()
        await self.db.refresh(session)
        return self._record_from_model(session, agent_profile)

    async def get_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSessionRecord:
        session = await self._owned_readable_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        return await self._record_with_agent(session)

    async def rename_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> RuntimeSessionRecord:
        session = await self._owned_active_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if session.source_type == "wecom":
            raise RuntimeSessionImmutableError(
                RuntimeSessionImmutableError.code
            )
        await self.assert_title_available(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            title=title,
        )
        session.title_cache = title
        await self.db.commit()
        await self.db.refresh(session)
        return await self._record_with_agent(session)

    async def assert_title_available(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> None:
        conflict = await self.db.execute(
            select(RuntimeSession.id)
            .where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id != runtime_session_id,
                RuntimeSession.status == "active",
                RuntimeSession.title_cache == title,
            )
            .limit(1)
        )
        if conflict.scalar_one_or_none() is not None:
            raise RuntimeSessionTitleConflictError("session title is already in use")

    async def delete_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        result = await self.db.execute(
            select(RuntimeSession).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status.in_(("active", "deleting", EXPERT_DELETED_SESSION_STATUS)),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise PermissionError("runtime session not found")
        if session.source_type == "wecom":
            raise RuntimeSessionImmutableError(
                RuntimeSessionImmutableError.code
            )
        active_run = await self.db.execute(
            select(RuntimeRun.id)
            .where(
                RuntimeRun.session_id == session.id,
                RuntimeRun.status.in_(("accepted", "pending", "running")),
            )
            .limit(1)
        )
        if active_run.scalar_one_or_none() is not None:
            raise RuntimeSessionActiveRunError("runtime session has an active run")
        run_ids = select(RuntimeRun.id).where(RuntimeRun.session_id == session.id)
        await self.db.execute(
            delete(ChatFileArtifact).where(
                or_(
                    ChatFileArtifact.session_id == session.id,
                    ChatFileArtifact.run_id.in_(run_ids),
                )
            )
        )
        await self.db.execute(delete(RuntimeRunEvent).where(RuntimeRunEvent.run_id.in_(run_ids)))
        await self.db.execute(delete(RuntimeRun).where(RuntimeRun.session_id == session.id))
        self.db.add(
            RuntimeAuditLog(
                org_id=org_id,
                actor_user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                action="runtime_session.deleted",
                target_type="runtime_session",
                target_id=runtime_session_id,
                detail={"message_count": int(session.message_count_cache)},
            )
        )
        session.status = "deleted"
        await self.db.commit()

    async def mark_session_deleting(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        result = await self.db.execute(
            select(RuntimeSession).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status.in_(("active", "deleting")),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise PermissionError("runtime session not found")
        if session.source_type == "wecom":
            raise RuntimeSessionImmutableError(
                RuntimeSessionImmutableError.code
            )
        active_run = await self.db.execute(
            select(RuntimeRun.id).where(
                RuntimeRun.session_id == session.id,
                RuntimeRun.status.in_(("accepted", "pending", "running")),
            ).limit(1)
        )
        if active_run.scalar_one_or_none() is not None:
            raise RuntimeSessionActiveRunError("runtime session has an active run")
        # Keep the admission transition and task pause in one transaction.  The
        # repository only stages the UPDATE; this method owns the sole commit.
        await ScheduledTaskRepository(self.db).pause_for_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            session_id=int(session.id),
        )
        session.status = "deleting"
        if session.context_source_runtime_session_id is not None:
            context_root = await self._context_root_session(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                session=session,
            )
            if (
                context_root is not None
                and context_root.context_head_runtime_session_id == runtime_session_id
            ):
                context_root.context_head_runtime_session_id = (
                    session.context_source_runtime_session_id
                )
        await self.db.commit()

    async def _context_root_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        session: RuntimeSession,
    ) -> RuntimeSession | None:
        current = session
        visited = {int(current.id)}
        while current.context_source_runtime_session_id is not None:
            parent = await self.db.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_session_id
                    == current.context_source_runtime_session_id,
                    RuntimeSession.status.in_(("active", EXPERT_DELETED_SESSION_STATUS)),
                )
            )
            if parent is None or int(parent.id) in visited:
                return None
            current = parent
            visited.add(int(current.id))
        return current

    async def mark_hermes_materialized(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        session = await self._owned_active_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if session.hermes_materialized_at is None:
            session.hermes_materialized_at = utc_now()
            await self.db.commit()

    async def _assert_workspace_owner(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> None:
        result = await self.db.execute(
            select(CloudHermesProfile.id).where(
                CloudHermesProfile.id == runtime_workspace_id,
                CloudHermesProfile.org_id == org_id,
                CloudHermesProfile.user_id == user_id,
                CloudHermesProfile.status == "active",
            )
        )
        if result.scalar_one_or_none() is None:
            raise PermissionError("runtime workspace not found")

    async def _resolve_agent_profile(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
    ) -> RuntimeAgentProfile:
        if not hasattr(self.db, "execute"):
            # Narrow compatibility seam for the repository's lightweight unit
            # stores. Production AsyncSession instances always take the
            # ownership-checked query below.
            return RuntimeAgentProfile(
                id=runtime_agent_profile_id or runtime_workspace_id,
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                hermes_profile_name="default",
                profile_kind="default",
                display_name="自己",
                ownership_type="default",
                default_workspace_key=runtime_workspace_id,
                status="active",
            )
        clauses = [
            RuntimeAgentProfile.org_id == org_id,
            RuntimeAgentProfile.user_id == user_id,
            RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
            *executable_runtime_agent_profile_clauses(),
        ]
        if runtime_agent_profile_id is None:
            clauses.append(RuntimeAgentProfile.profile_kind == "default")
        else:
            clauses.append(RuntimeAgentProfile.id == runtime_agent_profile_id)
        result = await self.db.execute(select(RuntimeAgentProfile).where(*clauses))
        profile = result.scalars().first()
        if profile is None:
            ownership_clauses = [
                RuntimeAgentProfile.org_id == org_id,
                RuntimeAgentProfile.user_id == user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.status == "active",
            ]
            if runtime_agent_profile_id is None:
                ownership_clauses.append(
                    RuntimeAgentProfile.profile_kind == "default"
                )
            else:
                ownership_clauses.append(
                    RuntimeAgentProfile.id == runtime_agent_profile_id
                )
            candidate = (
                await self.db.execute(
                    select(RuntimeAgentProfile).where(*ownership_clauses)
                )
            ).scalars().first()
            if (
                candidate is not None
                and int(candidate.desired_revision or 0) > 0
                and int(candidate.applied_revision or 0) == 0
            ):
                raise RuntimeSessionProfilePreparingError(
                    RuntimeSessionProfilePreparingError.code
                )
            raise PermissionError("runtime agent profile not found")
        return profile

    async def _owned_active_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSession:
        result = await self.db.execute(
            select(RuntimeSession).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status == "active",
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise PermissionError("runtime session not found")
        return session

    async def _owned_readable_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSession:
        result = await self.db.execute(
            select(RuntimeSession).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status.in_(("active", EXPERT_DELETED_SESSION_STATUS)),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise PermissionError("runtime session not found")
        return session

    async def _record_with_agent(
        self,
        session: RuntimeSession,
    ) -> RuntimeSessionRecord:
        agent_profile = await self.db.get(
            RuntimeAgentProfile,
            int(session.runtime_agent_profile_id),
        )
        channel = None
        if session.source_type == "wecom":
            channel = await self.db.scalar(
                select(ImChannel).where(
                    ImChannel.runtime_session_row_id == int(session.id)
                )
            )
        return self._record_from_model(
            session,
            agent_profile,
            scheduled_task_eligible=(
                personal_wecom_channel_is_scheduled_task_eligible(channel)
                if session.source_type == "wecom"
                else True
            ),
        )

    @staticmethod
    def _record_from_model(
        session: RuntimeSession,
        agent_profile: RuntimeAgentProfile | None = None,
        *,
        scheduled_task_eligible: bool = True,
        latest_run: RuntimeRun | None = None,
    ) -> RuntimeSessionRecord:
        return RuntimeSessionRecord(
            session_id=int(session.id),
            runtime_workspace_id=int(session.runtime_workspace_id),
            org_id=int(session.org_id),
            user_id=int(session.user_id),
            runtime_session_id=str(session.runtime_session_id),
            title=session.title_cache,
            source_type=session.source_type,
            message_count=int(session.message_count_cache),
            session_model_entry_id=int(session.session_model_entry_id) if session.session_model_entry_id else None,
            status=session.status,
            last_active_at=session.last_active_at,
            hermes_materialized_at=session.hermes_materialized_at,
            runtime_agent_profile_id=int(session.runtime_agent_profile_id),
            agent_display_name=(
                str(agent_profile.display_name) if agent_profile is not None else None
            ),
            agent_avatar_url=(
                agent_profile.avatar_url if agent_profile is not None else None
            ),
            agent_profile_kind=(
                str(agent_profile.profile_kind) if agent_profile is not None else None
            ),
            hermes_profile_name=(
                str(agent_profile.hermes_profile_name)
                if agent_profile is not None
                else None
            ),
            context_source_runtime_session_id=session.context_source_runtime_session_id,
            context_head_runtime_session_id=session.context_head_runtime_session_id,
            scheduled_task_eligible=scheduled_task_eligible,
            latest_run=(
                RuntimeSessionLatestRunRecord(
                    runtime_run_id=str(latest_run.runtime_run_id),
                    status=str(latest_run.status),
                    started_at=latest_run.started_at,
                    finished_at=latest_run.finished_at,
                )
                if latest_run is not None
                else None
            ),
        )
