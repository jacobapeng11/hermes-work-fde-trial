"""Execution orchestration for scheduled tasks.

The runner deliberately depends on narrow callables around the existing
Runtime services.  This keeps unit tests deterministic and, more importantly,
prevents a SQLAlchemy transaction from being held while a Runtime is waking or
while Hermes is streaming a response.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    CloudHermesProfile,
    ImChannel,
    RuntimeRun,
    RuntimeSession,
    ScheduledTask,
    User,
)
from app.im_gateway.identity import opaque_digest
from app.im_gateway.scheduled_delivery import (
    WECOM_SCHEDULED_TARGET_NOT_READY,
    personal_wecom_channel_is_scheduled_task_eligible,
)
from app.repositories.scheduled_task import ScheduledTaskRepository
from app.services.runtime_chat import (
    CreatedRuntimeRun,
    RuntimeChatActiveRunError,
    RuntimeChatPreparationError,
    RuntimeChatRequestIntent,
    RuntimeChatService,
    SqlAlchemyRuntimeChatStore,
)
from sqlalchemy import select
from app.services.runtime_workspace import Principal
from app.services.runtime_admission import RuntimeAdmissionError, RuntimeAdmissionService
from app.services.runtime_infrastructure import (
    build_runtime_infrastructure,
)
from app.services.scheduled_task_launcher import RunnerRequest, TaskSnapshot
from app.services.scheduled_task_execution_leases import (
    ScheduledTaskExecutionGrant,
    ScheduledTaskExecutionLeaseStore,
)
from app.services.scheduled_task_service import ScheduledTaskService
from app.services.runtime_internal_turns import (
    RuntimeInternalTurnError,
    resolve_personal_internal_turn,
    submit_personal_internal_turn,
)
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    deterministic_command_id,
    load_personal_runtime_command_target,
)
from app.services.runtime_maintenance import RuntimeMaintenanceError

logger = logging.getLogger(__name__)


class ScheduledTaskRunnerError(RuntimeError):
    """An execution could not be admitted or the Runtime was unavailable."""


class ScheduledTaskExecutionRecoveryPending(RuntimeError):
    """A taken-over Runtime Run has not reached a durable terminal state."""


class ScheduledTaskExecutionSuperseded(RuntimeError):
    """The captured trigger is no longer eligible for execution."""


class ScheduledTaskWeComTargetUnavailable(RuntimeError):
    code = WECOM_SCHEDULED_TARGET_NOT_READY


@dataclass(frozen=True)
class RuntimeRunTerminal:
    runtime_run_id: str
    status: str
    error_code: str | None = None
    error_message: str | None = None


TaskLoader = Callable[[RunnerRequest], Awaitable[Any | None] | Any | None]
RuntimeActivator = Callable[[Principal, TaskSnapshot], Awaitable[Any] | Any]
RunCreator = Callable[[Principal, TaskSnapshot, Any], Awaitable[CreatedRuntimeRun] | CreatedRuntimeRun]
RunExecutor = Callable[
    ...,
    Awaitable[RuntimeRunTerminal | Any] | RuntimeRunTerminal | Any,
]
SourcePreparer = Callable[
    [Principal, CreatedRuntimeRun, TaskSnapshot, str], Awaitable[None] | None
]
RunFailureHandler = Callable[
    [Principal, CreatedRuntimeRun, Exception], Awaitable[None] | None
]
TaskUpdater = Callable[
    [RunnerRequest, RuntimeRunTerminal], Awaitable[None] | None
]
UserValidator = Callable[[Principal], Awaitable[bool] | bool]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ScheduledTaskRunner:
    """Run one immutable trigger snapshot through Runtime admission.

    ``task_loader`` is invoked before Runtime activation and before each
    admission retry.  In production it should create/close its own DB session;
    in tests it is usually an async fake.  The remaining seams map directly to
    RuntimeWorkspaceService, RuntimeChatService and Runtime InternalTurn.
    """

    def __init__(
        self,
        *,
        task_loader: TaskLoader | None = None,
        runtime_activator: RuntimeActivator | None = None,
        run_creator: RunCreator | None = None,
        run_executor: RunExecutor | None = None,
        source_preparer: SourcePreparer | None = None,
        run_failure_handler: RunFailureHandler | None = None,
        task_updater: TaskUpdater | None = None,
        user_validator: UserValidator | None = None,
        session_retry_interval_seconds: float | None = None,
        session_admission_timeout_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
        # Compatibility aliases used by callers wiring the protocol seams.
        activate_runtime: RuntimeActivator | None = None,
        create_runtime_run: RunCreator | None = None,
        execute_runtime_run: RunExecutor | None = None,
        runtime_workspace_service: Any | None = None,
        runtime_warmup_runner: Any | None = None,
        runtime_keepalive_probe: Any | None = None,
        runtime_chat_service: Any | None = None,
    ) -> None:
        self.task_loader = task_loader
        self.runtime_workspace_service = runtime_workspace_service
        self.runtime_warmup_runner = runtime_warmup_runner
        self.runtime_keepalive_probe = runtime_keepalive_probe
        self.runtime_chat_service = runtime_chat_service
        self.runtime_activator = runtime_activator or activate_runtime
        if self.runtime_activator is None and runtime_workspace_service is not None:
            self.runtime_activator = self._activate_workspace
        self.run_creator = run_creator or create_runtime_run
        if self.run_creator is None and runtime_chat_service is not None:
            self.run_creator = self._create_chat_run
        self.run_executor = run_executor or execute_runtime_run
        self.source_preparer = source_preparer
        self.run_failure_handler = run_failure_handler
        self.task_updater = task_updater
        self.user_validator = user_validator
        self.session_retry_interval_seconds = (
            settings.SCHEDULED_TASK_SESSION_RETRY_INTERVAL_SECONDS
            if session_retry_interval_seconds is None
            else max(0.0, float(session_retry_interval_seconds))
        )
        self.session_admission_timeout_seconds = (
            settings.SCHEDULED_TASK_SESSION_ADMISSION_TIMEOUT_SECONDS
            if session_admission_timeout_seconds is None
            else max(0.0, float(session_admission_timeout_seconds))
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.success_count = 0
        self.failure_count = 0

    @classmethod
    def for_application(
        cls, *, broker: Any | None = None
    ) -> "ScheduledTaskRunner":
        """Build the real-process runner used by Scanner and manual trigger."""
        del broker

        async def execute_application_run(
            created: CreatedRuntimeRun,
            snapshot: TaskSnapshot,
            endpoint: Any,
            trigger_kind: str = "scheduled",
        ) -> RuntimeRunTerminal:
            executor = cls._execute_application_run
            parameters = inspect.signature(executor).parameters
            args = (created, snapshot, endpoint)
            if "trigger_kind" in parameters:
                return await executor(
                    *args,
                    trigger_kind=trigger_kind,
                )
            return await executor(*args)

        runner = cls(
            task_loader=cls._load_application_task,
            runtime_activator=cls._activate_application_runtime,
            run_creator=cls._create_application_run,
            run_executor=execute_application_run,
            source_preparer=cls._prepare_application_source,
            run_failure_handler=cls._fail_application_run,
            task_updater=cls._update_application_task,
        )
        return runner

    @staticmethod
    async def _update_application_task(request: RunnerRequest, terminal: RuntimeRunTerminal) -> None:
        async with AsyncSessionLocal() as db:
            service = ScheduledTaskService(ScheduledTaskRepository(db))
            await service.finalize_run(request=request, terminal=terminal)

    @staticmethod
    async def _load_application_task(request: RunnerRequest) -> Any | None:
        async with AsyncSessionLocal() as db:
            try:
                result = await db.execute(
                    select(ScheduledTask, RuntimeSession, User, CloudHermesProfile)
                    .join(RuntimeSession, RuntimeSession.id == ScheduledTask.target_session_id)
                    .join(User, User.id == ScheduledTask.user_id)
                    .join(CloudHermesProfile, CloudHermesProfile.id == ScheduledTask.runtime_workspace_id)
                    .where(
                        ScheduledTask.id == request.task_id,
                        ScheduledTask.org_id == request.task_snapshot.org_id,
                        ScheduledTask.user_id == request.task_snapshot.user_id,
                        ScheduledTask.runtime_workspace_id == request.task_snapshot.runtime_workspace_id,
                        ScheduledTask.status != "deleted",
                    )
                )
                row = result.first()
                if row is None:
                    return None
                task, session, user, profile = row
                if user.status != "active" or profile.status != "active":
                    return None
                if session.source_type == "wecom":
                    channel = await db.scalar(
                        select(ImChannel).where(
                            ImChannel.runtime_session_row_id == int(session.id)
                        )
                    )
                    if not personal_wecom_channel_is_scheduled_task_eligible(
                        channel
                    ):
                        raise ScheduledTaskWeComTargetUnavailable(
                            WECOM_SCHEDULED_TARGET_NOT_READY
                        )
                return ScheduledTaskRepository._record(task, session)
            except ScheduledTaskWeComTargetUnavailable:
                raise
            except Exception:
                return None

    @staticmethod
    async def _activate_application_runtime(principal: Principal, snapshot: TaskSnapshot) -> Any:
        async with AsyncSessionLocal() as db:
            infrastructure = build_runtime_infrastructure(db)
            store = infrastructure.workspace_store
            workspace = infrastructure.workspace_service
            profile = await workspace.ensure_default_for_user(principal)
            if profile.runtime_workspace_id != snapshot.runtime_workspace_id:
                raise ScheduledTaskRunnerError("runtime workspace ownership mismatch")
            if profile.runtime_status != "ready":
                try:
                    profile = await infrastructure.warmup_runner.warmup(profile)
                except RuntimeAdmissionError as exc:
                    raise ScheduledTaskRunnerError(exc.decision.code) from exc
            if profile.runtime_status != "ready":
                decision = RuntimeAdmissionService.decide(
                    str(profile.runtime_status or "")
                )
                raise ScheduledTaskRunnerError(decision.code)
            endpoint = await store.get_runtime_endpoint(profile)
        return endpoint

    @staticmethod
    async def _create_application_run(
        principal: Principal,
        snapshot: TaskSnapshot,
        _endpoint: Any,
        request: RunnerRequest | None = None,
    ) -> CreatedRuntimeRun:
        trigger_kind = request.trigger_kind if request is not None else "scheduled"
        scheduled_fire_at = (
            _as_utc(request.scheduled_fire_at)
            if request is not None
            else _as_utc(snapshot.next_run_at)
        )
        occurrence_key = (
            f"scheduled:{snapshot.task_id}:"
            f"{sha256(scheduled_fire_at.isoformat().encode('utf-8')).hexdigest()[:24]}"
            if trigger_kind == "scheduled" and scheduled_fire_at is not None
            else None
        )
        async with AsyncSessionLocal() as db:
            session = await db.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.id == snapshot.target_session_id,
                    RuntimeSession.org_id == principal.org_id,
                    RuntimeSession.user_id == principal.user_id,
                    RuntimeSession.runtime_workspace_id
                    == snapshot.runtime_workspace_id,
                    RuntimeSession.runtime_session_id
                    == snapshot.target_runtime_session_id,
                    RuntimeSession.status == "active",
                )
            )
            if session is None:
                raise ScheduledTaskRunnerError(
                    "scheduled target session is unavailable"
                )
            im_source: dict[str, object] = {}
            if session.source_type == "wecom":
                channel = await db.scalar(
                    select(ImChannel).where(
                        ImChannel.runtime_session_row_id == int(session.id)
                    )
                )
                if not personal_wecom_channel_is_scheduled_task_eligible(
                    channel
                ):
                    raise ScheduledTaskWeComTargetUnavailable(
                        WECOM_SCHEDULED_TARGET_NOT_READY
                    )
                assert channel is not None
                im_source = {
                    "im_channel_id": int(channel.id),
                    "im_bot_id_digest": opaque_digest(str(channel.bot_id)),
                    "im_target_digest": str(
                        channel.proactive_target_digest
                    ),
                }
            service = RuntimeChatService(SqlAlchemyRuntimeChatStore(db))
            return await service.create_run(
                principal=principal,
                intent=RuntimeChatRequestIntent(
                    runtime_workspace_id=snapshot.runtime_workspace_id,
                    runtime_session_id=snapshot.target_runtime_session_id,
                    message=snapshot.prompt,
                    source_type="scheduled",
                    runtime_run_id=(
                        f"rr-{occurrence_key}"
                        if occurrence_key is not None
                        else None
                    ),
                    source_snapshot={
                        "type": "scheduled",
                        "scheduled_task_id": snapshot.task_id,
                        "scheduled_task_name": snapshot.task_name,
                        "trigger_kind": trigger_kind,
                        "task_available": True,
                        **im_source,
                    },
                ),
            )

    @staticmethod
    async def _prepare_application_source(
        principal: Principal,
        created: CreatedRuntimeRun,
        snapshot: TaskSnapshot,
        trigger_kind: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            service = RuntimeChatService(SqlAlchemyRuntimeChatStore(db))
            await service.prepare_scheduled_source(
                principal=principal,
                created=created,
                scheduled_task_id=snapshot.task_id,
                scheduled_task_name=snapshot.task_name,
                trigger_kind=trigger_kind,
            )

    @staticmethod
    async def _fail_application_run(
        principal: Principal,
        created: CreatedRuntimeRun,
        exc: Exception,
    ) -> None:
        async with AsyncSessionLocal() as db:
            service = RuntimeChatService(SqlAlchemyRuntimeChatStore(db))
            await service.mark_runtime_run_finished(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_run_id=created.runtime_run_id,
                runtime_provider_run_id=None,
                status="failed",
                error_message=str(exc) or type(exc).__name__,
            )

    @staticmethod
    async def _coalesce_application_run(
        principal: Principal,
        created: CreatedRuntimeRun,
        canonical_run_id: str,
    ) -> None:
        """Close the control-plane row superseded by a queued candidate."""
        if canonical_run_id == created.runtime_run_id:
            return
        async with AsyncSessionLocal() as db:
            service = RuntimeChatService(SqlAlchemyRuntimeChatStore(db))
            await service.mark_canceled(
                principal=principal,
                runtime_run_id=created.runtime_run_id,
                runtime_provider_run_id=None,
            )

    @staticmethod
    async def _execute_application_run(
        created: CreatedRuntimeRun,
        snapshot: TaskSnapshot,
        endpoint: Any,
        *,
        broker: Any | None = None,
        trigger_kind: str = "scheduled",
    ) -> RuntimeRunTerminal:
        del broker
        principal = Principal(org_id=snapshot.org_id, user_id=snapshot.user_id)
        async with AsyncSessionLocal() as db:
            context = await resolve_personal_internal_turn(
                db,
                principal=principal,
                created=created,
                endpoint=endpoint,
            )
        try:
            occurrence_source = (
                _as_utc(snapshot.next_run_at).isoformat()
                if _as_utc(snapshot.next_run_at) is not None
                else created.runtime_run_id
            )
            occurrence_key = (
                f"{trigger_kind}:{snapshot.task_id}:"
                f"{sha256(occurrence_source.encode('utf-8')).hexdigest()[:24]}"
            )
            result = await submit_personal_internal_turn(
                context=context,
                created=created,
                prompt=snapshot.prompt,
                source="scheduled_task",
                execution_key=(
                    f"scheduled:{snapshot.task_id}:"
                    f"{created.runtime_run_id}"
                ),
                source_detail={
                    "type": "scheduled",
                    "scheduled_task_id": snapshot.task_id,
                    "scheduled_task_name": snapshot.task_name,
                    "trigger_kind": trigger_kind,
                    "task_available": True,
                },
                scheduled_task_id=snapshot.task_id,
                scheduled_occurrence_key=occurrence_key,
            )
        except RuntimeInternalTurnError as exc:
            if exc.code == "RUNTIME_TURN_SUBMISSION_UNCERTAIN":
                raise ScheduledTaskExecutionRecoveryPending from exc
            await ScheduledTaskRunner._fail_application_run(
                principal,
                created,
                exc,
            )
            return RuntimeRunTerminal(
                created.runtime_run_id,
                "failed",
                exc.code,
            )
        admission_state = str(
            result.get("admission_state") or "queued"
        ).lower()
        canonical_run_id = str(
            result.get("canonical_run_id") or created.runtime_run_id
        ).strip()
        if not canonical_run_id:
            canonical_run_id = created.runtime_run_id
        if bool(result.get("coalesced")):
            await ScheduledTaskRunner._coalesce_application_run(
                principal,
                created,
                canonical_run_id,
            )
        return RuntimeRunTerminal(
            canonical_run_id,
            "queued" if admission_state in {"queued", "running"} else "failed",
            (
                None
                if admission_state in {"queued", "running"}
                else admission_state.upper() or "RUNTIME_FAILED"
            ),
        )

    async def run(self, request: RunnerRequest) -> None:
        """Execute a trigger and contain ordinary failures within the Runner."""
        started_at = _as_utc(self.clock()) or datetime.now(timezone.utc)
        terminal: RuntimeRunTerminal | None = None
        grant = request.execution_grant()
        lease_heartbeat: asyncio.Task[None] | None = None
        eligible = False
        finalized = self.task_updater is None
        if grant is not None:
            if not await self._begin_execution_lease(grant):
                return
            current_task = asyncio.current_task()
            if current_task is not None:
                lease_heartbeat = asyncio.create_task(
                    self._heartbeat_execution_lease(
                        grant, parent=current_task
                    ),
                    name=f"scheduled-lease-{request.task_id}",
                )
        try:
            principal, current = await self._validate(request)
            if current is None:
                raise ScheduledTaskExecutionSuperseded
            eligible = True
            if grant is not None and request.claimed_runtime_run_id:
                terminal = await self._recover_claimed_runtime_run(
                    request=request,
                    principal=principal,
                )
            else:
                endpoint = await self.prepare_runtime(
                    principal=principal,
                    snapshot=request.task_snapshot,
                )
                created = await self.wait_for_session_admission(
                    request=request,
                    principal=principal,
                    endpoint=endpoint,
                )
                if created is None:
                    raise ScheduledTaskExecutionSuperseded
                if grant is not None and not await self._bind_execution_run(
                    grant, runtime_run_id=created.runtime_run_id
                ):
                    lost = ScheduledTaskRunnerError(
                        "scheduled task execution lease was lost"
                    )
                    await self.fail_runtime_run_before_execution(
                        principal=principal,
                        created=created,
                        error=lost,
                    )
                    raise ScheduledTaskExecutionRecoveryPending
                try:
                    await self.prepare_scheduled_source(
                        principal=principal,
                        created=created,
                        snapshot=request.task_snapshot,
                        trigger_kind=request.trigger_kind,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self.fail_runtime_run_before_execution(
                        principal=principal,
                        created=created,
                        error=exc,
                    )
                    terminal = RuntimeRunTerminal(
                        runtime_run_id=created.runtime_run_id,
                        status="failed",
                        error_code=self._error_code(exc),
                        error_message=str(exc) or type(exc).__name__,
                    )
                else:
                    terminal = await self.execute_runtime_run(
                        created=created,
                        snapshot=request.task_snapshot,
                        endpoint=endpoint,
                        trigger_kind=request.trigger_kind,
                    )
            if terminal.status in {"completed", "success", "queued"}:
                self.success_count += 1
            else:
                self.failure_count += 1
        except asyncio.CancelledError:
            # Cancellation is lifecycle control, not a failed scheduled run.
            await self._stop_execution_heartbeat(lease_heartbeat)
            raise
        except ScheduledTaskExecutionRecoveryPending:
            # Keep the lease non-terminal. A later scanner takeover resumes
            # observation of the already-created Runtime Run.
            pass
        except ScheduledTaskExecutionSuperseded:
            eligible = False
        except Exception as exc:
            self.failure_count += 1
            # Keep exception details out of logs: provider errors can contain
            # credentials or echoed prompt text.  ``error_code`` is stable and
            # the bounded, redacted summary is persisted by the finalizer.
            logger.error(
                "scheduled task runner failed",
                extra=self._log_fields(
                    request,
                    RuntimeRunTerminal("", "failed", self._error_code(exc), None),
                    started_at,
                ),
            )
            terminal = RuntimeRunTerminal(
                runtime_run_id="",
                status="failed",
                error_code=self._error_code(exc),
                error_message=str(exc) or type(exc).__name__,
            )
        if terminal is None:
            await self._stop_execution_heartbeat(lease_heartbeat)
            if grant is not None and not eligible:
                await self._supersede_execution_lease(grant)
            return
        if self.task_updater is not None:
            try:
                await _resolve(self.task_updater(request, terminal))
                finalized = True
            except Exception:
                # A summary write must not turn a completed Runtime run into an
                # unhandled Launcher task; the next scan still has the due row.
                logger.error("scheduled task finalizer failed", extra=self._log_fields(request, terminal, started_at))
        await self._stop_execution_heartbeat(lease_heartbeat)
        if grant is not None and finalized:
            await self._finish_execution_lease(
                grant,
                terminal=terminal,
            )
        logger.info("scheduled task runner finished", extra=self._log_fields(request, terminal, started_at))

    @staticmethod
    async def _stop_execution_heartbeat(
        task: asyncio.Task[None] | None,
    ) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @staticmethod
    def _lease_store(
        db, grant: ScheduledTaskExecutionGrant
    ) -> ScheduledTaskExecutionLeaseStore:
        return ScheduledTaskExecutionLeaseStore(
            db,
            owner_instance_id=grant.owner_instance_id,
            lease_ttl_seconds=(
                settings.SCHEDULED_TASK_EXECUTION_LEASE_SECONDS
            ),
        )

    async def _begin_execution_lease(
        self, grant: ScheduledTaskExecutionGrant
    ) -> bool:
        async with AsyncSessionLocal() as db:
            return await self._lease_store(db, grant).begin(
                grant, now=datetime.now(timezone.utc)
            )

    async def _bind_execution_run(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        runtime_run_id: str,
    ) -> bool:
        async with AsyncSessionLocal() as db:
            return await self._lease_store(db, grant).bind_runtime_run(
                grant, runtime_run_id=runtime_run_id
            )

    async def _heartbeat_execution_lease(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        parent: asyncio.Task[Any],
    ) -> None:
        loop = asyncio.get_running_loop()
        last_confirmed = loop.time()
        interval = min(
            settings.SCHEDULED_TASK_EXECUTION_HEARTBEAT_SECONDS,
            settings.SCHEDULED_TASK_EXECUTION_LEASE_SECONDS / 3,
        )
        while True:
            await asyncio.sleep(interval)
            retained: bool | None = None
            try:
                async with AsyncSessionLocal() as db:
                    retained = await self._lease_store(
                        db, grant
                    ).heartbeat(
                        grant, now=datetime.now(timezone.utc)
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient database failure gets the remainder of the last
                # confirmed TTL. Ownership loss itself is never retried.
                retained = None
            if retained is True:
                last_confirmed = loop.time()
                continue
            if retained is False or (
                loop.time() - last_confirmed
                >= settings.SCHEDULED_TASK_EXECUTION_LEASE_SECONDS
            ):
                if not parent.done():
                    parent.cancel(
                        "scheduled task execution lease lost"
                    )
                return

    async def _finish_execution_lease(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        terminal: RuntimeRunTerminal,
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await self._lease_store(db, grant).finish(
                    grant,
                    success=terminal.status in {
                        "completed",
                        "success",
                        "queued",
                    },
                    error_code=terminal.error_code,
                    now=datetime.now(timezone.utc),
                )
        except Exception:
            logger.error(
                "scheduled task execution lease finalization failed",
                extra={
                    "scheduled_task_id": grant.task_id,
                    "error_code": "LEASE_FINALIZATION_FAILED",
                },
            )

    async def _supersede_execution_lease(
        self, grant: ScheduledTaskExecutionGrant
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await self._lease_store(db, grant).supersede(
                    grant, now=datetime.now(timezone.utc)
                )
        except Exception:
            logger.error(
                "scheduled task superseded lease cleanup failed",
                extra={
                    "scheduled_task_id": grant.task_id,
                    "error_code": "LEASE_SUPERSEDE_FAILED",
                },
            )

    async def _recover_claimed_runtime_run(
        self,
        *,
        request: RunnerRequest,
        principal: Principal,
    ) -> RuntimeRunTerminal:
        runtime_run_id = str(
            request.claimed_runtime_run_id or ""
        ).strip()
        if not runtime_run_id:
            raise ScheduledTaskExecutionRecoveryPending
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(
            self.session_admission_timeout_seconds,
            float(settings.RUNTIME_RUN_RECONCILE_AFTER_SECONDS + 30),
        )
        while True:
            target = None
            async with AsyncSessionLocal() as db:
                row = await db.scalar(
                    select(RuntimeRun).where(
                        RuntimeRun.runtime_run_id == runtime_run_id,
                        RuntimeRun.org_id == principal.org_id,
                        RuntimeRun.user_id == principal.user_id,
                        RuntimeRun.runtime_workspace_id
                        == request.task_snapshot.runtime_workspace_id,
                    )
                )
                if row is not None and str(row.status or "").lower() not in {
                    "completed",
                    "failed",
                    "canceled",
                    "cancel_failed",
                }:
                    created_at = _as_utc(row.created_at)
                    now = _as_utc(self.clock()) or datetime.now(timezone.utc)
                    reconcile_after = max(
                        0,
                        int(settings.RUNTIME_RUN_RECONCILE_AFTER_SECONDS),
                    )
                    old_enough = (
                        created_at is None
                        or (now - created_at).total_seconds()
                        >= reconcile_after
                    )
                    if old_enough:
                        try:
                            target = await load_personal_runtime_command_target(
                                db,
                                org_id=principal.org_id,
                                user_id=principal.user_id,
                                runtime_workspace_id=(
                                    request.task_snapshot.runtime_workspace_id
                                ),
                            )
                        except (
                            RuntimeCommandError,
                            RuntimeMaintenanceError,
                        ) as exc:
                            raise ScheduledTaskExecutionRecoveryPending from exc
            if row is None:
                return RuntimeRunTerminal(
                    runtime_run_id=runtime_run_id,
                    status="failed",
                    error_code="RUNTIME_RUN_MISSING",
                    error_message=(
                        "The claimed Runtime Run is unavailable"
                    ),
                )
            status = str(row.status or "").lower()
            if status == "completed":
                return RuntimeRunTerminal(
                    runtime_run_id=runtime_run_id,
                    status="completed",
                )
            if status in {
                "failed",
                "canceled",
                "cancel_failed",
            }:
                return RuntimeRunTerminal(
                    runtime_run_id=runtime_run_id,
                    status="failed",
                    error_code=(
                        str(row.error_code or status).upper()[:100]
                    ),
                    error_message=row.error_message,
                )
            if target is not None:
                client = RuntimeCommandClient()
                try:
                    try:
                        supervisor_run = await client.get_run(
                            target=target,
                            run_id=runtime_run_id,
                        )
                    except RuntimeCommandError as exc:
                        raise ScheduledTaskExecutionRecoveryPending from exc
                finally:
                    await client.close()
                if supervisor_run is None:
                    return await self._resubmit_missing_supervisor_run(
                        request=request,
                        principal=principal,
                        row=row,
                    )
            if loop.time() >= deadline:
                raise ScheduledTaskExecutionRecoveryPending
            await asyncio.sleep(
                min(2.0, max(0.0, deadline - loop.time()))
            )

    async def _resubmit_missing_supervisor_run(
        self,
        *,
        request: RunnerRequest,
        principal: Principal,
        row: RuntimeRun,
    ) -> RuntimeRunTerminal:
        """Re-admit an old control-plane Run only after an explicit 404."""

        payload = (
            dict(row.request_payload)
            if isinstance(row.request_payload, dict)
            else {}
        )
        raw_source = payload.get("source")
        source_detail = (
            dict(raw_source) if isinstance(raw_source, dict) else {}
        )
        raw_profile_id = payload.get("runtime_agent_profile_id")
        try:
            runtime_agent_profile_id = (
                int(raw_profile_id) if raw_profile_id is not None else None
            )
        except (TypeError, ValueError):
            runtime_agent_profile_id = None
        created = CreatedRuntimeRun(
            runtime_workspace_id=int(row.runtime_workspace_id),
            runtime_session_id=str(row.runtime_session_id or ""),
            runtime_run_id=str(row.runtime_run_id),
            model_entry_id=(
                int(row.model_entry_id)
                if row.model_entry_id is not None
                else None
            ),
            status=str(row.status),
            runtime_agent_profile_id=runtime_agent_profile_id,
            hermes_profile_name=str(
                payload.get("hermes_profile_name") or "default"
            ),
        )
        prompt = payload.get("message")
        scheduled_fire_at = _as_utc(request.scheduled_fire_at)
        try:
            source_task_id = int(source_detail.get("scheduled_task_id"))
        except (TypeError, ValueError):
            source_task_id = -1
        valid_context = (
            isinstance(prompt, str)
            and bool(prompt.strip())
            and created.runtime_session_id
            == request.task_snapshot.target_runtime_session_id
            and source_detail.get("type") == "scheduled"
            and source_task_id == request.task_id
            and source_detail.get("trigger_kind") == request.trigger_kind
            and request.trigger_kind == "scheduled"
            and scheduled_fire_at is not None
        )
        if not valid_context:
            error = RuntimeInternalTurnError(
                "RUNTIME_RUN_RECOVERY_CONTEXT_INVALID"
            )
            await self._fail_application_run(principal, created, error)
            return RuntimeRunTerminal(
                created.runtime_run_id,
                "failed",
                error.code,
            )

        try:
            endpoint = await self.prepare_runtime(
                principal=principal,
                snapshot=request.task_snapshot,
            )
            async with AsyncSessionLocal() as db:
                context = await resolve_personal_internal_turn(
                    db,
                    principal=principal,
                    created=created,
                    endpoint=endpoint,
                )
            execution_key = (
                f"scheduled:{request.task_id}:{created.runtime_run_id}"
            )
            occurrence_key = (
                f"scheduled:{request.task_id}:"
                f"{sha256(scheduled_fire_at.isoformat().encode('utf-8')).hexdigest()[:24]}"
            )
            result = await submit_personal_internal_turn(
                context=context,
                created=created,
                prompt=prompt,
                source="scheduled_task",
                execution_key=execution_key,
                source_detail=source_detail,
                scheduled_task_id=request.task_id,
                scheduled_occurrence_key=occurrence_key,
                command_id_override=deterministic_command_id(
                    "internal-turn-recovery",
                    "scheduled_task",
                    context.target.binding_id,
                    execution_key,
                ),
            )
        except RuntimeInternalTurnError as exc:
            if exc.code == "RUNTIME_TURN_SUBMISSION_UNCERTAIN":
                raise ScheduledTaskExecutionRecoveryPending from exc
            await self._fail_application_run(principal, created, exc)
            return RuntimeRunTerminal(
                created.runtime_run_id,
                "failed",
                exc.code,
            )
        except (RuntimeCommandError, RuntimeMaintenanceError) as exc:
            raise ScheduledTaskExecutionRecoveryPending from exc
        except ScheduledTaskRunnerError as exc:
            raise ScheduledTaskExecutionRecoveryPending from exc

        admission_state = str(
            result.get("admission_state") or "queued"
        ).lower()
        canonical_run_id = str(
            result.get("canonical_run_id") or created.runtime_run_id
        ).strip() or created.runtime_run_id
        if bool(result.get("coalesced")):
            await self._coalesce_application_run(
                principal,
                created,
                canonical_run_id,
            )
        return RuntimeRunTerminal(
            canonical_run_id,
            "queued" if admission_state in {"queued", "running"} else "failed",
            (
                None
                if admission_state in {"queued", "running"}
                else admission_state.upper() or "RUNTIME_FAILED"
            ),
        )

    async def _validate(self, request: RunnerRequest) -> tuple[Principal, Any | None]:
        snapshot = request.task_snapshot
        principal = Principal(org_id=snapshot.org_id, user_id=snapshot.user_id)
        if self.user_validator is not None and not await _resolve(self.user_validator(principal)):
            return principal, None
        if self.task_loader is None:
            # A factory-less runner is useful as a no-op in contract mode, but
            # must never submit a prompt based solely on a stale snapshot.
            return principal, None
        current = await _resolve(self.task_loader(request))
        if current is None or not self._eligible(request, current):
            return principal, None
        return principal, current

    @staticmethod
    def _eligible(request: RunnerRequest, current: Any) -> bool:
        status = str(getattr(current, "status", ""))
        session_status = getattr(current, "target_session_status", None)
        if session_status in {"deleting", "deleted"}:
            return False
        if request.trigger_kind == "manual":
            return status != "deleted"
        if status != "active":
            return False
        expected = _as_utc(request.scheduled_fire_at)
        actual = _as_utc(getattr(current, "next_run_at", None))
        return expected is not None and actual == expected

    async def prepare_runtime(self, *, principal: Principal, snapshot: TaskSnapshot) -> Any:
        """Activate a ready or cold Runtime through the supplied seam."""
        if self.runtime_activator is None:
            raise ScheduledTaskRunnerError("runtime activator is not configured")
        return await _resolve(self.runtime_activator(principal, snapshot))

    async def _activate_workspace(self, principal: Principal, snapshot: TaskSnapshot) -> Any:
        """Use the existing RuntimeWorkspace service for warmup/keepalive.

        The service's store owns short DB transactions.  No object returned by
        this method is a live SQLAlchemy session.
        """
        service = self.runtime_workspace_service
        profile = await _resolve(service.ensure_default_for_user(principal))
        if int(getattr(profile, "runtime_workspace_id", -1)) != snapshot.runtime_workspace_id:
            raise ScheduledTaskRunnerError("runtime workspace ownership mismatch")
        if str(getattr(profile, "runtime_status", "")) in {
            "force_stopped",
            "force_stop_requested",
            "force_stop_failed",
            "restart_requested",
        }:
            raise ScheduledTaskRunnerError("runtime is administratively stopped")
        store = getattr(service, "store", None)
        if store is None:
            raise ScheduledTaskRunnerError("runtime workspace store is unavailable")
        if str(getattr(profile, "runtime_status", "")) == "ready":
            endpoint = await _resolve(store.get_runtime_endpoint(profile))
            if self.runtime_keepalive_probe is not None:
                await _resolve(self.runtime_keepalive_probe.keepalive(endpoint))
            return endpoint
        if self.runtime_warmup_runner is None:
            raise ScheduledTaskRunnerError("runtime warmup is not configured")
        warmed = await _resolve(service.warmup_default_for_user(principal, self.runtime_warmup_runner))
        endpoint = await _resolve(store.get_runtime_endpoint(warmed or profile))
        if self.runtime_keepalive_probe is not None:
            await _resolve(self.runtime_keepalive_probe.keepalive(endpoint))
        return endpoint

    async def _create_chat_run(
        self, principal: Principal, snapshot: TaskSnapshot, _endpoint: Any
    ) -> CreatedRuntimeRun:
        intent = RuntimeChatRequestIntent(
            runtime_workspace_id=snapshot.runtime_workspace_id,
            runtime_session_id=snapshot.target_runtime_session_id,
            message=snapshot.prompt,
            source_type="scheduled",
        )
        return await _resolve(self.runtime_chat_service.create_run(principal=principal, intent=intent))

    async def prepare_scheduled_runtime_run(
        self, *, principal: Principal, snapshot: TaskSnapshot
    ) -> CreatedRuntimeRun:
        """Compatibility helper that performs activation and one admission."""
        endpoint = await self.prepare_runtime(principal=principal, snapshot=snapshot)
        request = RunnerRequest(snapshot.task_id, "scheduled", snapshot.next_run_at, snapshot)
        created = await self.wait_for_session_admission(request=request, principal=principal, endpoint=endpoint)
        if created is None:
            raise ScheduledTaskRunnerError("scheduled task was no longer eligible")
        return created

    async def wait_for_session_admission(
        self,
        *,
        request: RunnerRequest,
        principal: Principal | None = None,
        endpoint: Any | None = None,
    ) -> CreatedRuntimeRun | None:
        """Create one control-plane Run; Supervisor owns Session admission."""
        if principal is None:
            principal = Principal(
                org_id=request.task_snapshot.org_id,
                user_id=request.task_snapshot.user_id,
            )
        if endpoint is None:
            endpoint = await self.prepare_runtime(principal=principal, snapshot=request.task_snapshot)
        if self.user_validator is not None and not await _resolve(
            self.user_validator(principal)
        ):
            return None
        if self.task_loader is not None:
            current = await _resolve(self.task_loader(request))
            if current is None or not self._eligible(request, current):
                return None
        if self.run_creator is None:
            raise ScheduledTaskRunnerError("run creator is not configured")
        creator = self.run_creator
        parameters = inspect.signature(creator).parameters.values()
        accepts_request = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        ) or len(
            [
                parameter
                for parameter in parameters
                if parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                }
            ]
        ) >= 4
        args = (principal, request.task_snapshot, endpoint)
        return await _resolve(
            creator(*args, request) if accepts_request else creator(*args)
        )

    async def execute_runtime_run(
        self,
        *,
        created: CreatedRuntimeRun,
        snapshot: TaskSnapshot,
        endpoint: Any,
        trigger_kind: str = "scheduled",
    ) -> RuntimeRunTerminal:
        if self.run_executor is None:
            raise ScheduledTaskRunnerError("run executor is not configured")
        executor = self.run_executor
        parameters = inspect.signature(executor).parameters.values()
        accepts_trigger_kind = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        ) or len(
            [
                parameter
                for parameter in parameters
                if parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                }
            ]
        ) >= 4
        args = (created, snapshot, endpoint)
        result = await _resolve(
            executor(*args, trigger_kind)
            if accepts_trigger_kind
            else executor(*args)  # type: ignore[call-arg]
        )
        if isinstance(result, RuntimeRunTerminal):
            return result
        status = str(getattr(result, "status", result or "completed"))
        return RuntimeRunTerminal(
            runtime_run_id=str(getattr(result, "runtime_run_id", created.runtime_run_id)),
            status=status,
            error_message=getattr(result, "error_message", None),
        )

    async def prepare_scheduled_source(
        self,
        *,
        principal: Principal,
        created: CreatedRuntimeRun,
        snapshot: TaskSnapshot,
        trigger_kind: str,
    ) -> None:
        preparer = self.source_preparer
        if preparer is not None:
            await _resolve(preparer(principal, created, snapshot, trigger_kind))
            return
        service = self.runtime_chat_service
        method = getattr(service, "prepare_scheduled_source", None) if service is not None else None
        if callable(method):
            await _resolve(
                method(
                    principal=principal,
                    created=created,
                    snapshot=snapshot,
                    trigger_kind=trigger_kind,
                )
            )

    async def fail_runtime_run_before_execution(
        self,
        *,
        principal: Principal,
        created: CreatedRuntimeRun,
        error: Exception,
    ) -> None:
        handler = self.run_failure_handler
        if handler is not None:
            await _resolve(handler(principal, created, error))
            return
        service = self.runtime_chat_service
        method = getattr(service, "mark_runtime_run_finished", None) if service is not None else None
        if callable(method):
            await _resolve(
                method(
                    org_id=principal.org_id,
                    user_id=principal.user_id,
                    runtime_run_id=created.runtime_run_id,
                    runtime_provider_run_id=None,
                    status="failed",
                    error_message=str(error) or type(error).__name__,
                )
            )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        explicit_code = getattr(exc, "code", None)
        if isinstance(explicit_code, str) and explicit_code:
            return explicit_code[:100]
        if isinstance(exc, RuntimeChatActiveRunError):
            return "SESSION_BUSY"
        if isinstance(exc, RuntimeChatPreparationError):
            return "RUNTIME_RUN_PREPARATION_FAILED"
        return type(exc).__name__.upper()

    @staticmethod
    def _log_fields(request: RunnerRequest, terminal: RuntimeRunTerminal, started_at: datetime) -> dict[str, Any]:
        snapshot = request.task_snapshot
        return {
            "scheduled_task_id": snapshot.task_id,
            "org_id": snapshot.org_id,
            "user_id": snapshot.user_id,
            "profile_id": snapshot.runtime_workspace_id,
            "runtime_workspace_id": snapshot.runtime_workspace_id,
            "runtime_id": None,
            "target_session_id": snapshot.target_session_id,
            "target_runtime_session_id": snapshot.target_runtime_session_id,
            "runtime_run_id": terminal.runtime_run_id or None,
            "scheduled_at": snapshot.next_run_at,
            "started_at": started_at,
            "finished_at": _as_utc(datetime.now(timezone.utc)),
            "result": (
                "success"
                if terminal.status in {"success", "completed", "queued"}
                else "failed"
            ),
            "error_code": terminal.error_code,
            "trigger_kind": request.trigger_kind,
            "scheduled_fire_at": request.scheduled_fire_at,
        }

    @property
    def metrics(self) -> dict[str, int]:
        return {"runner_success_count": self.success_count, "runner_failure_count": self.failure_count}


async def run_scheduled_task(request: RunnerRequest, *, runner: ScheduledTaskRunner) -> None:
    """Small factory-friendly entrypoint for ``ScheduledTaskLauncher``."""
    await runner.run(request)


async def wait_for_session_admission(
    *, request: RunnerRequest, runner: ScheduledTaskRunner
) -> CreatedRuntimeRun | None:
    return await runner.wait_for_session_admission(request=request)


async def execute_runtime_run(
    *, created: CreatedRuntimeRun, snapshot: TaskSnapshot, endpoint: Any, runner: ScheduledTaskRunner
) -> RuntimeRunTerminal:
    return await runner.execute_runtime_run(created=created, snapshot=snapshot, endpoint=endpoint)
