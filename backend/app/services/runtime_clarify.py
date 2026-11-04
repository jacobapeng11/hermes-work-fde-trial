from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertKanbanCollaboration,
    ExpertKanbanCollaborationTask,
    HermesRuntimeBinding,
    RuntimeAgentProfile,
    RuntimeClarifyRequest,
    RuntimeRun,
    RuntimeSession,
)
from app.schemas.runtime_internal import CancelCommandPayload
from app.schemas.runtime_clarify import RuntimeClarifyAnswer, RuntimeClarifyCreateRequest
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    RuntimeCommandTarget,
    build_runtime_command,
    deterministic_command_id,
    load_runtime_command_admission_generation,
)
from app.services.runtime_chat import (
    RuntimeChatRequestIntent,
    RuntimeChatService,
    SqlAlchemyRuntimeChatStore,
)
from app.services.expert_kanban_worker_runs import (
    ExpertKanbanWorkerRunService,
)
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_internal_turns import (
    RuntimeInternalTurnError,
    resolve_personal_internal_turn,
    submit_personal_internal_turn,
)
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec


logger = logging.getLogger(__name__)
ACTIVE_RUN_STATUSES = frozenset({"pending", "accepted", "running"})
PARENT_TERMINAL_WAIT_SECONDS = 15.0
PARENT_TERMINAL_POLL_SECONDS = 0.1
_TERMINAL_COLLABORATION_STATES = frozenset(
    {"delivered", "delivery_failed", "canceled"}
)


class RuntimeClarifyError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RuntimeClarifyReceipt:
    enabled: bool
    request: RuntimeClarifyRequest | None
    duplicate: bool = False
    mode: str = "web_run"


class RuntimeClarifyService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        timeout_seconds: int = 3_600,
        auto_default_seconds: int = 60,
        claim_lease_seconds: int = 120,
        codec: RuntimeApiKeyCodec | None = None,
        command_client_factory: Callable[[], RuntimeCommandClient] | None = None,
    ) -> None:
        self.session = session
        # Legacy expiry is intentionally not applied to new requests.
        self.timeout_seconds = int(timeout_seconds)
        self.auto_default_seconds = int(auto_default_seconds)
        self.claim_lease_seconds = int(claim_lease_seconds)
        self.codec = codec or RuntimeApiKeyCodec()
        self.command_client_factory = command_client_factory or RuntimeCommandClient
        self.parent_terminal_wait_seconds = PARENT_TERMINAL_WAIT_SECONDS

    async def accept_runtime_request(
        self,
        *,
        runtime_workspace_id: int,
        runtime_token: str,
        payload: RuntimeClarifyCreateRequest,
        now: datetime | None = None,
    ) -> RuntimeClarifyReceipt:
        now = now or datetime.utcnow()
        binding = await self._authenticate(runtime_workspace_id, runtime_token)
        session = await self.session.scalar(
            select(RuntimeSession).where(
                RuntimeSession.org_id == binding.org_id,
                RuntimeSession.user_id == binding.user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == payload.runtime_session_id,
            )
        )
        if session is None or str(session.source_type) != "web":
            if payload.runtime_agent_profile_id is not None:
                return await self._accept_expert_worker_request(
                    binding=binding,
                    runtime_workspace_id=runtime_workspace_id,
                    payload=payload,
                    now=now,
                )
            return RuntimeClarifyReceipt(enabled=False, request=None)
        run = await self.session.scalar(
            select(RuntimeRun)
            .where(
                RuntimeRun.session_id == session.id,
                RuntimeRun.status == "running",
                RuntimeRun.source_type == "web",
            )
            .order_by(RuntimeRun.id.desc())
        )
        if run is None:
            # The Supervisor outbox is FIFO, but the plugin can reach this
            # endpoint just before `run.accepted` is materialized.  Failing
            # closed here keeps Hermes from entering its blocking callback;
            # the user can retry rather than receive a stuck session.
            return RuntimeClarifyReceipt(enabled=False, request=None)
        existing = await self.session.scalar(
            select(RuntimeClarifyRequest).where(
                RuntimeClarifyRequest.runtime_workspace_id == runtime_workspace_id,
                RuntimeClarifyRequest.event_id == payload.event_id,
            )
        )
        if existing is not None:
            return RuntimeClarifyReceipt(
                enabled=existing.status == "waiting",
                request=existing,
                duplicate=True,
            )
        active = await self.session.scalar(
            select(RuntimeClarifyRequest).where(
                RuntimeClarifyRequest.session_id == session.id,
                RuntimeClarifyRequest.status.in_((
                    "waiting",
                    "claimed",
                    "resume_failed",
                )),
            )
        )
        if active is not None:
            return RuntimeClarifyReceipt(enabled=False, request=None)
        choices = [item.strip() for item in payload.choices if item.strip()][:4]
        auto_default_at = (
            None
            if self.auto_default_seconds <= 0
            else now + timedelta(seconds=self.auto_default_seconds)
        )
        request = RuntimeClarifyRequest(
            org_id=int(binding.org_id),
            user_id=int(binding.user_id),
            runtime_workspace_id=runtime_workspace_id,
            session_id=int(session.id),
            parent_run_id=int(run.id),
            request_id=f"clr_{uuid4().hex}",
            event_id=payload.event_id,
            runtime_session_id=payload.runtime_session_id,
            tool_call_id=payload.tool_call_id,
            question=payload.question.strip(),
            choices=choices or None,
            multi_select=bool(payload.multi_select and choices),
            auto_default_at=auto_default_at,
            expires_at=None,
        )
        self.session.add(request)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.session.scalar(
                select(RuntimeClarifyRequest).where(
                    RuntimeClarifyRequest.runtime_workspace_id == runtime_workspace_id,
                    RuntimeClarifyRequest.event_id == payload.event_id,
                )
            )
            if existing is None:
                raise
            return RuntimeClarifyReceipt(
                enabled=existing.status == "waiting",
                request=existing,
                duplicate=True,
            )
        if not await self._cancel_parent_run(
            binding=binding,
            run=run,
            request_id=request.request_id,
        ):
            # Do not expose a card when its original turn may still consume
            # the marker. A later user turn can safely request clarification
            # again, while this failed attempt remains auditable.
            request.status = "canceled"
            request.canceled_at = now
            request.version += 1
            await self.session.commit()
            return RuntimeClarifyReceipt(enabled=False, request=request)
        return RuntimeClarifyReceipt(enabled=True, request=request)

    async def _accept_expert_worker_request(
        self,
        *,
        binding: HermesRuntimeBinding,
        runtime_workspace_id: int,
        payload: RuntimeClarifyCreateRequest,
        now: datetime,
    ) -> RuntimeClarifyReceipt:
        """Persist a card for a native Kanban expert worker.

        The worker is already parked inside its ``pre_tool_call`` hook, so no
        cancellation or continuation Run is involved: the plugin polls the
        stored answer and injects it into the same turn.
        """
        existing = await self.session.scalar(
            select(RuntimeClarifyRequest).where(
                RuntimeClarifyRequest.runtime_workspace_id == runtime_workspace_id,
                RuntimeClarifyRequest.event_id == payload.event_id,
            )
        )
        if existing is not None:
            return RuntimeClarifyReceipt(
                enabled=existing.status == "waiting",
                request=existing,
                duplicate=True,
                mode=str(existing.mode or "web_run"),
            )
        context = await self._resolve_expert_worker_context(
            binding=binding,
            runtime_workspace_id=runtime_workspace_id,
            payload=payload,
        )
        if context is None:
            return RuntimeClarifyReceipt(
                enabled=False,
                request=None,
                mode="expert_worker",
            )
        profile, collaboration, task, worker_run, chat_session = context
        active = await self.session.scalar(
            select(RuntimeClarifyRequest).where(
                RuntimeClarifyRequest.session_id == chat_session.id,
                RuntimeClarifyRequest.status.in_((
                    "waiting",
                    "claimed",
                    "resume_failed",
                )),
            )
        )
        if active is not None:
            return RuntimeClarifyReceipt(
                enabled=False,
                request=None,
                mode="expert_worker",
            )
        choices = [item.strip() for item in payload.choices if item.strip()][:4]
        auto_default_at = (
            None
            if self.auto_default_seconds <= 0
            else now + timedelta(seconds=self.auto_default_seconds)
        )
        request = RuntimeClarifyRequest(
            org_id=int(binding.org_id),
            user_id=int(binding.user_id),
            runtime_workspace_id=runtime_workspace_id,
            session_id=int(chat_session.id),
            parent_run_id=int(worker_run.id),
            request_id=f"clr_{uuid4().hex}",
            event_id=payload.event_id,
            runtime_session_id=str(chat_session.runtime_session_id),
            tool_call_id=payload.tool_call_id,
            question=payload.question.strip(),
            choices=choices or None,
            multi_select=bool(payload.multi_select and choices),
            auto_default_at=auto_default_at,
            expires_at=None,
            mode="expert_worker",
            collaboration_task_id=int(task.id),
            expert_profile_id=int(profile.id),
            requested_by=str(profile.display_name or "专家")[:128],
        )
        self.session.add(request)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self.session.scalar(
                select(RuntimeClarifyRequest).where(
                    RuntimeClarifyRequest.runtime_workspace_id == runtime_workspace_id,
                    RuntimeClarifyRequest.event_id == payload.event_id,
                )
            )
            if existing is None:
                raise
            return RuntimeClarifyReceipt(
                enabled=existing.status == "waiting",
                request=existing,
                duplicate=True,
                mode=str(existing.mode or "web_run"),
            )
        return RuntimeClarifyReceipt(
            enabled=True,
            request=request,
            mode="expert_worker",
        )

    async def _resolve_expert_worker_context(
        self,
        *,
        binding: HermesRuntimeBinding,
        runtime_workspace_id: int,
        payload: RuntimeClarifyCreateRequest,
    ):
        profile = await self.session.scalar(
            select(RuntimeAgentProfile).where(
                RuntimeAgentProfile.id == int(payload.runtime_agent_profile_id or 0),
                RuntimeAgentProfile.org_id == binding.org_id,
                RuntimeAgentProfile.user_id == binding.user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.profile_kind == "expert",
                *executable_runtime_agent_profile_clauses(),
            )
        )
        if profile is None:
            return None
        expert_key = f"expert-{int(profile.id)}"
        rows = (
            await self.session.execute(
                select(ExpertKanbanCollaborationTask, ExpertKanbanCollaboration)
                .join(
                    ExpertKanbanCollaboration,
                    ExpertKanbanCollaboration.id
                    == ExpertKanbanCollaborationTask.collaboration_id,
                )
                .where(
                    ExpertKanbanCollaborationTask.runtime_workspace_id
                    == runtime_workspace_id,
                    ExpertKanbanCollaboration.state.not_in(
                        _TERMINAL_COLLABORATION_STATES
                    ),
                )
                .order_by(ExpertKanbanCollaborationTask.id)
                .limit(100)
            )
        ).all()
        for task, collaboration in rows:
            worker_run_id = ExpertKanbanWorkerRunService.runtime_run_id(
                runtime_workspace_id=runtime_workspace_id,
                board=str(task.board or "default"),
                task_id=str(task.task_id),
            )
            run = await self.session.scalar(
                select(RuntimeRun).where(RuntimeRun.runtime_run_id == worker_run_id)
            )
            if run is None or str(run.status) not in ACTIVE_RUN_STATUSES:
                continue
            if (
                str((run.request_payload or {}).get("expert_profile_name") or "")
                != expert_key
            ):
                continue
            chat_session = await self.session.get(
                RuntimeSession, int(collaboration.session_id)
            )
            if chat_session is None or str(chat_session.source_type) != "web":
                continue
            return profile, collaboration, task, run, chat_session
        return None

    async def _cancel_parent_run(
        self,
        *,
        binding: HermesRuntimeBinding,
        run: RuntimeRun,
        request_id: str,
    ) -> bool:
        """Interrupt Hermes before returning a card to the browser.

        Hermes suppresses Dashboard tool callbacks when a pre-tool hook blocks
        execution. The persisted clarify request is therefore the reliable
        boundary from which to issue the Supervisor cancellation command.
        """

        runtime_run_id = str(run.runtime_run_id or "").strip()
        endpoint_host_header = str(binding.endpoint_host_header or "").strip()
        if not runtime_run_id or not endpoint_host_header:
            return False
        admission_generation = await load_runtime_command_admission_generation(
            self.session
        )
        target = RuntimeCommandTarget(
            runtime_id=str(binding.runtime_id),
            binding_id=str(binding.id),
            endpoint_host_header=endpoint_host_header,
            api_server_key=self.codec.decrypt(str(binding.api_key_encrypted)),
            admission_generation=admission_generation,
        )
        client = self.command_client_factory()
        try:
            envelope = build_runtime_command(
                target=target,
                command_id=deterministic_command_id(
                    "clarify-park-cancel",
                    target.binding_id,
                    request_id,
                    runtime_run_id,
                ),
                payload=CancelCommandPayload(
                    run_id=runtime_run_id,
                    clarify_request_id=request_id,
                ),
                deadline_seconds=30,
            )
            ack = await client.send(target=target, envelope=envelope)
            return ack.state == "completed" and ack.run_id == runtime_run_id
        except RuntimeCommandError:
            return False
        finally:
            await client.close()

    async def list_for_session(self, *, principal: Principal, runtime_workspace_id: int, runtime_session_id: str, now: datetime | None = None) -> list[RuntimeClarifyRequest]:
        # Legacy expires_at values no longer terminate a request. Terminal rows
        # remain readable so old clients can render them instead of hiding them.
        rows = await self.session.execute(
            select(RuntimeClarifyRequest)
            .where(
                RuntimeClarifyRequest.org_id == principal.org_id,
                RuntimeClarifyRequest.user_id == principal.user_id,
                RuntimeClarifyRequest.runtime_workspace_id == runtime_workspace_id,
                RuntimeClarifyRequest.runtime_session_id == runtime_session_id,
            )
            .order_by(RuntimeClarifyRequest.id)
        )
        return list(rows.scalars())

    async def answer(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        request_id: str,
        answer: RuntimeClarifyAnswer,
        version: int,
        now: datetime | None = None,
    ) -> RuntimeClarifyRequest:
        now = now or datetime.utcnow()
        request = await self._owned_request(principal, runtime_workspace_id, request_id)
        self._validate_answer(request, answer)
        payload = answer.model_dump(mode="json")

        if request.status in {"claimed", "resume_failed", "answered"}:
            if request.answer != payload:
                raise RuntimeClarifyError("clarify_already_answered", status_code=409)
            if request.status == "resume_failed":
                return await self._retry_claim(request, payload, now=now)
            return request
        if request.status != "waiting":
            raise RuntimeClarifyError("clarify_not_waiting", status_code=409)
        if int(request.version) != int(version):
            raise RuntimeClarifyError("clarify_version_conflict", status_code=409)

        values = self._claim_values(request, payload, source="user", now=now)
        result = await self.session.execute(
            update(RuntimeClarifyRequest)
            .where(
                RuntimeClarifyRequest.id == request.id,
                RuntimeClarifyRequest.status == "waiting",
                RuntimeClarifyRequest.version == int(version),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            current = await self._owned_request(
                principal,
                runtime_workspace_id,
                request_id,
            )
            if current.answer != payload:
                raise RuntimeClarifyError(
                    "clarify_already_answered",
                    status_code=409,
                )
            if current.status == "resume_failed":
                return await self._retry_claim(current, payload, now=now)
            return current
        await self.session.commit()
        await self.session.refresh(request)
        return request

    async def cancel(self, *, principal: Principal, runtime_workspace_id: int, request_id: str, now: datetime | None = None) -> RuntimeClarifyRequest:
        request = await self._owned_request(principal, runtime_workspace_id, request_id)
        if request.status == "waiting":
            request.status = "canceled"
            request.canceled_at = now or datetime.utcnow()
            request.version += 1
            await self.session.commit()
        return request

    async def resume_claimed_request(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        request: RuntimeClarifyRequest,
        chat_service: RuntimeChatService,
        now: datetime | None = None,
    ) -> RuntimeClarifyRequest:
        """Submit one deterministic continuation and only then close the card."""

        now = now or datetime.utcnow()
        if request.status == "answered":
            return request
        if request.status != "claimed":
            return request

        parent = await self.session.scalar(
            select(RuntimeRun).where(RuntimeRun.id == request.parent_run_id)
        )
        if parent is None:
            return await self._mark_resume_failed(
                request,
                error_code="CLARIFY_PARENT_RUN_NOT_FOUND",
                now=now,
            )
        answer_prompt = self._continuation_prompt(request)
        # End the read transaction before any Runtime command. MySQL retains
        # only claim/retry state while Supervisor owns admission.
        await self.session.commit()
        try:
            if not await self._wait_for_parent_terminal(parent):
                return await self._mark_resume_failed(
                    request,
                    error_code="CLARIFY_PARENT_RUN_STILL_ACTIVE",
                    now=now,
                )
            await self.session.refresh(request)
            await self.session.commit()
            canonical_run_id = await self._submit_continuation(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                request=request,
                parent=parent,
                prompt=answer_prompt,
                chat_service=chat_service,
            )
        except RuntimeInternalTurnError as exc:
            if exc.code == "RUNTIME_TURN_SUBMISSION_UNCERTAIN":
                await self.session.rollback()
                await self.session.refresh(request)
                logger.warning(
                    "clarify continuation uncertain request_id=%s error_code=%s",
                    request.request_id,
                    exc.code,
                )
                return request
            return await self._mark_resume_failed(
                request,
                error_code=exc.code,
                now=now,
            )
        except Exception as exc:
            return await self._mark_resume_failed(
                request,
                error_code=str(getattr(exc, "code", None) or type(exc).__name__),
                now=now,
            )
        return await self._mark_answered(request, canonical_run_id)

    async def _wait_for_parent_terminal(self, parent: RuntimeRun) -> bool:
        """Wait for the cancellation ACK to be projected into MySQL.

        The Runtime command can complete before Backend consumes the terminal
        projection. Creating the continuation immediately would therefore see
        the old active parent and reject an otherwise valid user selection.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.parent_terminal_wait_seconds
        while True:
            # Refresh opens a read transaction. Roll back before sleeping so
            # the next poll observes the projection committed by another
            # session instead of retaining the old REPEATABLE READ snapshot.
            await self.session.rollback()
            await self.session.refresh(parent)
            if parent.status not in ACTIVE_RUN_STATUSES:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self.session.rollback()
                return False
            await asyncio.sleep(min(PARENT_TERMINAL_POLL_SECONDS, remaining))

    async def resume_answered_request(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        request: RuntimeClarifyRequest,
        chat_service: RuntimeChatService,
    ) -> RuntimeClarifyRequest:
        """Compatibility entry for callers that still name the legacy state."""

        return await self.resume_claimed_request(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            request=request,
            chat_service=chat_service,
        )

    async def finalize_expert_worker_request(
        self,
        request: RuntimeClarifyRequest,
    ) -> RuntimeClarifyRequest:
        """Close an expert worker answer in place; no continuation Run exists."""
        if request.status == "answered":
            return request
        if request.status != "claimed":
            return request
        result = await self.session.execute(
            update(RuntimeClarifyRequest)
            .where(
                RuntimeClarifyRequest.id == request.id,
                RuntimeClarifyRequest.status == "claimed",
                RuntimeClarifyRequest.version == int(request.version),
            )
            .values(
                status="answered",
                resume_run_id=None,
                claim_expires_at=None,
                next_resume_at=None,
                version=int(request.version) + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            await self.session.refresh(request)
            return request
        await self.session.commit()
        await self.session.refresh(request)
        return request

    async def runtime_request_status(
        self,
        *,
        runtime_workspace_id: int,
        request_id: str,
        runtime_token: str,
    ) -> RuntimeClarifyRequest | None:
        binding = await self._authenticate(runtime_workspace_id, runtime_token)
        return await self.session.scalar(
            select(RuntimeClarifyRequest).where(
                RuntimeClarifyRequest.runtime_workspace_id
                == binding.runtime_workspace_id,
                RuntimeClarifyRequest.request_id == request_id,
            )
        )

    async def _submit_continuation(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        request: RuntimeClarifyRequest,
        parent: RuntimeRun,
        prompt: str,
        chat_service: RuntimeChatService,
    ) -> str:
        created = await chat_service.create_run(
            principal=principal,
            intent=RuntimeChatRequestIntent(
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=request.runtime_session_id,
                message=prompt,
                model_entry_id=parent.model_entry_id,
                source_type="web",
                channel="web",
                runtime_run_id=request.resume_run_id,
            ),
        )
        endpoint = await chat_service.get_runtime_endpoint(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
        )
        context = await resolve_personal_internal_turn(
            self.session,
            principal=principal,
            created=created,
            endpoint=endpoint,
        )
        await self.session.commit()
        result = await submit_personal_internal_turn(
            context=context,
            created=created,
            prompt=prompt,
            source="web_clarify",
            execution_key=request.request_id,
            source_detail={
                "request_id": request.request_id,
                "answer_source": request.answer_source,
            },
        )
        canonical_run_id = str(result.get("canonical_run_id") or "").strip()
        return canonical_run_id or str(created.runtime_run_id)

    def _claim_values(
        self,
        request: RuntimeClarifyRequest,
        answer: dict,
        *,
        source: str,
        now: datetime,
    ) -> dict:
        resume_run_id = (
            str(request.resume_run_id or "").strip()
            or f"rr-clarify-{int(request.id)}-{request.request_id[-12:]}"
        )
        return {
            "status": "claimed",
            "answer": answer,
            "answer_source": source,
            "answered_at": now,
            "claim_expires_at": now + timedelta(seconds=self.claim_lease_seconds),
            "resume_attempt_count": int(request.resume_attempt_count or 0) + 1,
            "resume_run_id": resume_run_id,
            "next_resume_at": None,
            "version": int(request.version) + 1,
        }

    async def _retry_claim(
        self,
        request: RuntimeClarifyRequest,
        answer: dict,
        *,
        now: datetime,
    ) -> RuntimeClarifyRequest:
        result = await self.session.execute(
            update(RuntimeClarifyRequest)
            .where(
                RuntimeClarifyRequest.id == request.id,
                RuntimeClarifyRequest.status == "resume_failed",
                RuntimeClarifyRequest.version == int(request.version),
            )
            .values(
                status="claimed",
                claim_expires_at=now + timedelta(seconds=self.claim_lease_seconds),
                resume_attempt_count=int(request.resume_attempt_count or 0) + 1,
                next_resume_at=None,
                version=int(request.version) + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            await self.session.refresh(request)
            if request.answer != answer:
                raise RuntimeClarifyError("clarify_already_answered", status_code=409)
            return request
        await self.session.commit()
        await self.session.refresh(request)
        return request

    async def _mark_resume_failed(
        self,
        request: RuntimeClarifyRequest,
        *,
        error_code: str,
        now: datetime,
    ) -> RuntimeClarifyRequest:
        await self.session.rollback()
        await self.session.refresh(request)
        attempts = int(request.resume_attempt_count or 1)
        delay = min(300, 5 * (2 ** max(0, attempts - 1)))
        result = await self.session.execute(
            update(RuntimeClarifyRequest)
            .where(
                RuntimeClarifyRequest.id == request.id,
                RuntimeClarifyRequest.status == "claimed",
                RuntimeClarifyRequest.version == int(request.version),
            )
            .values(
                status="resume_failed",
                next_resume_at=now + timedelta(seconds=delay),
                version=int(request.version) + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            await self.session.refresh(request)
            return request
        await self.session.commit()
        await self.session.refresh(request)
        logger.warning(
            "clarify continuation failed request_id=%s error_code=%s attempt=%s",
            request.request_id,
            str(error_code)[:128],
            attempts,
        )
        return request

    async def _mark_answered(
        self,
        request: RuntimeClarifyRequest,
        canonical_run_id: str | None,
    ) -> RuntimeClarifyRequest:
        resume_run_id = (
            str(canonical_run_id or "").strip()
            or str(request.resume_run_id or "").strip()
        )
        result = await self.session.execute(
            update(RuntimeClarifyRequest)
            .where(
                RuntimeClarifyRequest.id == request.id,
                RuntimeClarifyRequest.status == "claimed",
                RuntimeClarifyRequest.version == int(request.version),
            )
            .values(
                status="answered",
                resume_run_id=resume_run_id,
                claim_expires_at=None,
                next_resume_at=None,
                version=int(request.version) + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            await self.session.refresh(request)
            return request
        await self.session.commit()
        await self.session.refresh(request)
        return request

    async def claim_auto_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 25,
    ) -> list[RuntimeClarifyRequest]:
        now = now or datetime.utcnow()
        rows = (
            await self.session.execute(
                select(RuntimeClarifyRequest)
                .where(
                    RuntimeClarifyRequest.status == "waiting",
                    RuntimeClarifyRequest.auto_default_at.is_not(None),
                    RuntimeClarifyRequest.auto_default_at <= now,
                )
                .order_by(RuntimeClarifyRequest.auto_default_at, RuntimeClarifyRequest.id)
                .limit(int(limit))
            )
        ).scalars()
        return await self._claim_rows(
            list(rows),
            answer={"kind": "skip", "text": None, "values": []},
            source="auto",
            now=now,
        )

    async def claim_retryable(
        self,
        *,
        now: datetime | None = None,
        limit: int = 25,
        max_attempts: int = 12,
    ) -> list[RuntimeClarifyRequest]:
        now = now or datetime.utcnow()
        rows = (
            await self.session.execute(
                select(RuntimeClarifyRequest)
                .where(
                    or_(
                        (RuntimeClarifyRequest.status == "claimed")
                        & (
                            or_(
                                RuntimeClarifyRequest.claim_expires_at.is_(None),
                                RuntimeClarifyRequest.claim_expires_at <= now,
                            )
                        ),
                        (RuntimeClarifyRequest.status == "resume_failed")
                        & (RuntimeClarifyRequest.next_resume_at <= now)
                        & (
                            RuntimeClarifyRequest.resume_attempt_count
                            < int(max_attempts)
                        ),
                    ),
                    RuntimeClarifyRequest.answer.is_not(None),
                    RuntimeClarifyRequest.resume_run_id.is_not(None),
                )
                .order_by(RuntimeClarifyRequest.next_resume_at, RuntimeClarifyRequest.id)
                .limit(int(limit))
            )
        ).scalars()
        claimed: list[RuntimeClarifyRequest] = []
        for row in list(rows):
            result = await self.session.execute(
                update(RuntimeClarifyRequest)
                .where(
                    RuntimeClarifyRequest.id == row.id,
                    RuntimeClarifyRequest.status == row.status,
                    RuntimeClarifyRequest.version == int(row.version),
                )
                .values(
                    status="claimed",
                    claim_expires_at=now + timedelta(seconds=self.claim_lease_seconds),
                    resume_attempt_count=int(row.resume_attempt_count or 0) + 1,
                    next_resume_at=None,
                    version=int(row.version) + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await self.session.rollback()
                continue
            await self.session.commit()
            await self.session.refresh(row)
            claimed.append(row)
        return claimed

    async def _claim_rows(
        self,
        rows: list[RuntimeClarifyRequest],
        *,
        answer: dict,
        source: str,
        now: datetime,
    ) -> list[RuntimeClarifyRequest]:
        claimed: list[RuntimeClarifyRequest] = []
        for row in rows:
            values = self._claim_values(row, answer, source=source, now=now)
            result = await self.session.execute(
                update(RuntimeClarifyRequest)
                .where(
                    RuntimeClarifyRequest.id == row.id,
                    RuntimeClarifyRequest.status == "waiting",
                    RuntimeClarifyRequest.version == int(row.version),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await self.session.rollback()
                continue
            await self.session.commit()
            await self.session.refresh(row)
            claimed.append(row)
        return claimed

    async def _authenticate(self, runtime_workspace_id: int, runtime_token: str) -> HermesRuntimeBinding:
        binding = await self.session.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                HermesRuntimeBinding.status.in_(("starting", "ready")),
            )
        )
        if binding is None:
            raise RuntimeClarifyError("runtime_clarify_unauthorized", status_code=401)
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimeClarifyError("runtime_clarify_unauthorized", status_code=401)
        return binding

    async def _owned_request(self, principal: Principal, runtime_workspace_id: int, request_id: str) -> RuntimeClarifyRequest:
        request = await self.session.scalar(
            select(RuntimeClarifyRequest).where(
                RuntimeClarifyRequest.request_id == request_id,
                RuntimeClarifyRequest.org_id == principal.org_id,
                RuntimeClarifyRequest.user_id == principal.user_id,
                RuntimeClarifyRequest.runtime_workspace_id == runtime_workspace_id,
            )
        )
        if request is None:
            raise RuntimeClarifyError("clarify_not_found", status_code=404)
        return request

    @staticmethod
    def _validate_answer(request: RuntimeClarifyRequest, answer: RuntimeClarifyAnswer) -> None:
        choices = list(request.choices or [])
        if answer.kind == "multi":
            if not request.multi_select:
                raise RuntimeClarifyError("clarify_answer_shape_invalid")
            if any(value not in choices for value in answer.values):
                raise RuntimeClarifyError("clarify_answer_choice_invalid")
        elif answer.kind == "single" and answer.text not in choices:
            raise RuntimeClarifyError("clarify_answer_choice_invalid")

    @staticmethod
    def _continuation_prompt(request: RuntimeClarifyRequest) -> str:
        answer = request.answer or {}
        kind = str(answer.get("kind") or "text")
        if kind == "multi":
            rendered = "、".join(str(value) for value in answer.get("values", []))
        elif kind == "skip":
            rendered = "用户跳过了该问题，请自行采用合理默认值。"
        else:
            rendered = str(answer.get("text") or "")
        return (
            "请继续完成上一轮用户任务。上一轮已因澄清问题停驻；"
            "不要再次询问同一个问题，并把以下用户答案作为已确认上下文。\n\n"
            f"澄清问题：{request.question}\n"
            f"用户答案：{rendered}\n\n"
            "现在基于这项答案继续执行并向用户给出结果。"
        )


class RuntimeClarifyScanner:
    """Lifespan-owned scanner for auto-default and bounded continuation retry."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        chat_service_factory: Callable[[AsyncSession], RuntimeChatService] | None = None,
        auto_default_seconds: int = 60,
        claim_lease_seconds: int = 120,
        max_auto_resume_attempts: int = 12,
        batch_size: int = 25,
        interval_seconds: float = 5,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.chat_service_factory = chat_service_factory or (
            lambda db: RuntimeChatService(SqlAlchemyRuntimeChatStore(db))
        )
        self.auto_default_seconds = int(auto_default_seconds)
        self.claim_lease_seconds = int(claim_lease_seconds)
        self.max_auto_resume_attempts = int(max_auto_resume_attempts)
        self.batch_size = int(batch_size)
        self.interval_seconds = float(interval_seconds)
        self.clock = clock or datetime.utcnow
        self._task: asyncio.Task[None] | None = None
        self.last_scan_at: datetime | None = None
        self.scan_error_count = 0
        self.auto_default_count = 0
        self.user_answer_count = 0
        self.resume_success_count = 0
        self.resume_failed_count = 0
        self.uncertain_count = 0
        self.expert_worker_answered_count = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.scan_error_count += 1
                logger.exception("runtime clarify scan failed")
            await asyncio.sleep(self.interval_seconds)

    async def scan_once(self, *, now: datetime | None = None) -> int:
        scan_time = now or self.clock()
        self.last_scan_at = scan_time
        async with self.session_factory() as db:
            service = RuntimeClarifyService(
                db,
                auto_default_seconds=self.auto_default_seconds,
                claim_lease_seconds=self.claim_lease_seconds,
            )
            due = await service.claim_auto_due(now=scan_time, limit=self.batch_size)
            retries = await service.claim_retryable(
                now=scan_time,
                limit=self.batch_size,
                max_attempts=self.max_auto_resume_attempts,
            )
        self.auto_default_count += len(due)
        self.user_answer_count += sum(
            row.answer_source == "user" for row in retries
        )

        attempted = 0
        for claimed in (*due, *retries):
            async with self.session_factory() as db:
                request = await db.scalar(
                    select(RuntimeClarifyRequest).where(
                        RuntimeClarifyRequest.request_id == claimed.request_id
                    )
                )
                if request is None or request.status != "claimed":
                    continue
                service = RuntimeClarifyService(
                    db,
                    auto_default_seconds=self.auto_default_seconds,
                    claim_lease_seconds=self.claim_lease_seconds,
                )
                before_status = str(request.status)
                try:
                    if str(request.mode or "web_run") == "expert_worker":
                        result = await service.finalize_expert_worker_request(
                            request
                        )
                    else:
                        result = await service.resume_claimed_request(
                            principal=Principal(
                                org_id=int(request.org_id),
                                user_id=int(request.user_id),
                            ),
                            runtime_workspace_id=int(request.runtime_workspace_id),
                            request=request,
                            chat_service=self.chat_service_factory(db),
                            now=scan_time,
                        )
                except Exception:
                    self.scan_error_count += 1
                    logger.exception(
                        "runtime clarify continuation scan failed request_id=%s",
                        request.request_id,
                    )
                    continue
                attempted += 1
                if result.status == "answered":
                    if str(request.mode or "web_run") == "expert_worker":
                        self.expert_worker_answered_count += 1
                    else:
                        self.resume_success_count += 1
                elif result.status == "resume_failed":
                    self.resume_failed_count += 1
                elif before_status == "claimed" and result.status == "claimed":
                    self.uncertain_count += 1
        return attempted

    @property
    def metrics(self) -> dict[str, int | datetime | None]:
        return {
            "last_scan_at": self.last_scan_at,
            "scan_error_count": self.scan_error_count,
            "auto_default_count": self.auto_default_count,
            "user_answer_count": self.user_answer_count,
            "resume_success_count": self.resume_success_count,
            "resume_failed_count": self.resume_failed_count,
            "uncertain_count": self.uncertain_count,
            "expert_worker_answered_count": self.expert_worker_answered_count,
        }
