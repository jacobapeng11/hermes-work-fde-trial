"""Backend-owned keepalive leases for Hermes' native Kanban board.

The receipt table and lease row are control-plane facts only.  Task existence
and terminal state are always read from Hermes through its public Dashboard
Kanban API; no task state is copied into the platform database.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    HermesRuntimeBinding,
    RuntimeAgentProfile,
    RuntimeKanbanLease,
    RuntimeKanbanLeaseEvent,
)
from app.schemas.runtime_kanban import RuntimeKanbanLeaseEventRequest
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_workspace import RuntimeApiKeyCodec


logger = logging.getLogger(__name__)
_KANBAN_TOOLS = {
    "kanban_create",
    "kanban_claim",
    "kanban_complete",
    "kanban_block",
    "kanban_unblock",
    "kanban_heartbeat",
    "kanban_link",
    "kanban_comment",
}


class RuntimeKanbanLeaseError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RuntimeKanbanLeaseReceipt:
    duplicate: bool
    lease_state: str


@dataclass(frozen=True)
class _LeaseSnapshot:
    lease_id: int
    workspace_id: int
    endpoint: RuntimeEndpoint


class RuntimeKanbanLeaseService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        codec: RuntimeApiKeyCodec | None = None,
        temporary_seconds: int = 120,
    ) -> None:
        self.session = session
        self.codec = codec or RuntimeApiKeyCodec()
        self.temporary_seconds = max(15, int(temporary_seconds))

    async def accept_event(
        self,
        *,
        runtime_workspace_id: int,
        runtime_token: str,
        event: RuntimeKanbanLeaseEventRequest,
        now: datetime | None = None,
    ) -> RuntimeKanbanLeaseReceipt:
        now = now or datetime.utcnow()
        binding = await self._authenticate(
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=event.runtime_agent_profile_id,
            runtime_token=runtime_token,
        )
        if event.event_name in {"pre_tool_call", "post_tool_call"}:
            if event.tool_name not in _KANBAN_TOOLS:
                raise RuntimeKanbanLeaseError("runtime_kanban_tool_not_allowed")

        duplicate = await self._record_once(binding=binding, event=event)
        lease = await self.session.scalar(
            select(RuntimeKanbanLease).where(
                RuntimeKanbanLease.runtime_workspace_id == runtime_workspace_id
            )
        )
        if duplicate:
            return RuntimeKanbanLeaseReceipt(
                duplicate=True,
                lease_state=str(lease.state) if lease is not None else "released",
            )

        if lease is None:
            lease = RuntimeKanbanLease(
                org_id=int(binding.org_id),
                user_id=int(binding.user_id),
                runtime_workspace_id=runtime_workspace_id,
                state="temporary",
                temporary_expires_at=now + timedelta(seconds=self.temporary_seconds),
                next_reconcile_at=now,
            )
            self.session.add(lease)

        is_create_pre = event.event_name == "pre_tool_call" and event.tool_name == "kanban_create"
        is_create_failure = (
            event.event_name == "post_tool_call"
            and event.tool_name == "kanban_create"
            and event.outcome not in {None, "ok"}
        )
        if is_create_pre:
            lease.state = "temporary"
            lease.temporary_expires_at = now + timedelta(seconds=self.temporary_seconds)
            lease.release_after = None
        elif is_create_failure:
            lease.state = "reconciling"
            lease.temporary_expires_at = now
        else:
            # A successful create or any durable Kanban lifecycle transition
            # requires an authoritative board read.  Do not infer task state
            # from this observer event.
            lease.state = "reconciling"
            lease.temporary_expires_at = None
            lease.release_after = None
        lease.next_reconcile_at = now
        lease.last_error = None
        await self.session.commit()
        return RuntimeKanbanLeaseReceipt(duplicate=False, lease_state=str(lease.state))

    async def _authenticate(
        self,
        *,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> HermesRuntimeBinding:
        binding = await self.session.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                HermesRuntimeBinding.status.in_(("starting", "ready")),
            )
        )
        if binding is None:
            raise RuntimeKanbanLeaseError("runtime_kanban_unauthorized", status_code=401)
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimeKanbanLeaseError("runtime_kanban_unauthorized", status_code=401)
        if runtime_agent_profile_id is not None:
            profile_id = await self.session.scalar(
                select(RuntimeAgentProfile.id).where(
                    RuntimeAgentProfile.id == runtime_agent_profile_id,
                    RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                    RuntimeAgentProfile.org_id == binding.org_id,
                    RuntimeAgentProfile.user_id == binding.user_id,
                    *executable_runtime_agent_profile_clauses(),
                )
            )
            if profile_id is None:
                raise RuntimeKanbanLeaseError("runtime_kanban_unauthorized", status_code=401)
        return binding

    async def _record_once(
        self,
        *,
        binding: HermesRuntimeBinding,
        event: RuntimeKanbanLeaseEventRequest,
    ) -> bool:
        existing = await self.session.scalar(
            select(RuntimeKanbanLeaseEvent.id).where(
                RuntimeKanbanLeaseEvent.runtime_workspace_id
                == int(binding.runtime_workspace_id),
                RuntimeKanbanLeaseEvent.event_id == event.event_id,
            )
        )
        if existing is not None:
            return True
        try:
            async with self.session.begin_nested():
                self.session.add(
                    RuntimeKanbanLeaseEvent(
                        org_id=int(binding.org_id),
                        user_id=int(binding.user_id),
                        runtime_workspace_id=int(binding.runtime_workspace_id),
                        runtime_agent_profile_id=event.runtime_agent_profile_id,
                        event_id=event.event_id,
                        event_name=event.event_name,
                        tool_name=event.tool_name,
                        task_id=event.task_id,
                        board=event.board,
                        outcome=event.outcome,
                        runtime_session_id=event.runtime_session_id,
                        tool_call_id=event.tool_call_id,
                        turn_id=event.turn_id,
                        assignee=event.assignee,
                        profile_name=event.profile_name,
                    )
                )
                await self.session.flush()
        except IntegrityError:
            return True
        return False


class RuntimeKanbanLeaseReconciler:
    """Poll due native Boards; each poll also keeps the Knative route active."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        profile_adapter: HermesProfileAdapter,
        interval_seconds: int = 30,
        release_grace_seconds: int = 60,
    ) -> None:
        self.session_factory = session_factory
        self.profile_adapter = profile_adapter
        self.interval_seconds = max(5, int(interval_seconds))
        self.release_grace_seconds = max(0, int(release_grace_seconds))
        self.codec = RuntimeApiKeyCodec()

    async def reconcile_once(self, *, now: datetime | None = None) -> int:
        now = now or datetime.utcnow()
        snapshots = await self._due_snapshots(now)
        for snapshot in snapshots:
            try:
                active = await self.profile_adapter.has_any_executable_kanban_task(
                    snapshot.endpoint
                )
            except Exception as exc:
                await self._record_failure(snapshot.lease_id, now=now, exc=exc)
                continue
            await self._apply_observation(snapshot.lease_id, active=active, now=now)
        return len(snapshots)

    async def _due_snapshots(self, now: datetime) -> list[_LeaseSnapshot]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(RuntimeKanbanLease, HermesRuntimeBinding)
                    .join(
                        HermesRuntimeBinding,
                        HermesRuntimeBinding.runtime_workspace_id
                        == RuntimeKanbanLease.runtime_workspace_id,
                    )
                    .where(
                        RuntimeKanbanLease.next_reconcile_at <= now,
                        HermesRuntimeBinding.status == "ready",
                        HermesRuntimeBinding.endpoint_base_url.is_not(None),
                        HermesRuntimeBinding.endpoint_host_header.is_not(None),
                    )
                    .order_by(RuntimeKanbanLease.id)
                )
            ).all()
            return [
                _LeaseSnapshot(
                    lease_id=int(lease.id),
                    workspace_id=int(lease.runtime_workspace_id),
                    endpoint=RuntimeEndpoint(
                        runtime_workspace_id=int(binding.runtime_workspace_id),
                        runtime_id=str(binding.runtime_id),
                        endpoint_base_url=str(binding.endpoint_base_url),
                        endpoint_host_header=str(binding.endpoint_host_header),
                        api_key=self.codec.decrypt(str(binding.api_key_encrypted)),
                    ),
                )
                for lease, binding in rows
            ]

    async def _apply_observation(self, lease_id: int, *, active: bool, now: datetime) -> None:
        async with self.session_factory() as session:
            lease = await session.get(RuntimeKanbanLease, lease_id)
            if lease is None:
                return
            lease.last_reconciled_at = now
            lease.last_error = None
            if active:
                lease.state = "active"
                lease.temporary_expires_at = None
                lease.release_after = None
                lease.next_reconcile_at = now + timedelta(seconds=self.interval_seconds)
            elif lease.release_after is None:
                lease.state = "release_pending"
                lease.release_after = now + timedelta(seconds=self.release_grace_seconds)
                lease.next_reconcile_at = lease.release_after
            elif lease.release_after <= now:
                await session.delete(lease)
            else:
                lease.next_reconcile_at = lease.release_after
            await session.commit()

    async def _record_failure(self, lease_id: int, *, now: datetime, exc: Exception) -> None:
        logger.warning(
            "Runtime Kanban lease reconcile failed lease_id=%s error_type=%s",
            lease_id,
            type(exc).__name__,
        )
        async with self.session_factory() as session:
            lease = await session.get(RuntimeKanbanLease, lease_id)
            if lease is None:
                return
            lease.last_error = type(exc).__name__[:200]
            lease.next_reconcile_at = now + timedelta(seconds=self.interval_seconds)
            await session.commit()
