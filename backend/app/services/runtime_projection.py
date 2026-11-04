from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import logging

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentStudioSession,
    ChatFileArtifact,
    HermesRuntimeBinding,
    ImChannel,
    ImOutboundDelivery,
    ModelEntry,
    ModelUsageLog,
    RuntimeRun,
    RuntimeRunEvent,
    RuntimeProjectionReceipt,
    RuntimeSession,
    ScheduledTask,
    Skill,
    SkillVersion,
)
from app.im_gateway.identity import opaque_digest
from app.im_gateway.scheduled_delivery import (
    personal_wecom_channel_is_scheduled_task_eligible,
)
from app.models.runtime_agent_profile import RuntimeAgentProfile
from app.services.chat_files import (
    ChatFileError,
    ChatFilePermissionError,
    normalize_workspace_relative_path,
    session_scoped_relative_path,
    session_workspace_relative_root,
)
from app.services.application_agent_studio_chat_files import (
    ApplicationAgentStudioChatFileService,
)
from app.schemas.runtime_internal import (
    RuntimeProjectionBatch,
    RuntimeProjectionItem,
    RuntimeProjectionRejection,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec


logger = logging.getLogger(__name__)


class RuntimeProjectionAuthenticationError(RuntimeError):
    pass


class RuntimeProjectionBindingError(RuntimeError):
    pass


class RuntimeProjectionArtifactError(RuntimeError):
    """An authenticated Runtime sent an artifact outside its Run scope."""


class RuntimeProjectionFileError(RuntimeError):
    """An authenticated Runtime sent a file result outside its upload intent."""


def _public_source_detail(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    public = dict(value)
    public.pop("runtime_attachments", None)
    return public


@dataclass(frozen=True)
class RuntimeProjectionResult:
    accepted_projection_ids: list[str]
    rejected: list[RuntimeProjectionRejection]


class RuntimeProjectionService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        codec: RuntimeApiKeyCodec | None = None,
        materialize: bool = True,
    ) -> None:
        self.db = db
        self.codec = codec or RuntimeApiKeyCodec()
        self.materialize = materialize
        self._personal_binding: HermesRuntimeBinding | None = None
        self._application_binding: ApplicationAgentRuntimeBinding | None = None

    async def authenticate(
        self,
        *,
        runtime_id: str,
        binding_id: str,
        api_server_key: str,
    ) -> None:
        binding = await self.db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_id == runtime_id
            )
        )
        self._personal_binding = binding
        if binding is None:
            binding = await self.db.scalar(
                select(ApplicationAgentRuntimeBinding).where(
                    ApplicationAgentRuntimeBinding.runtime_id == runtime_id
                )
            )
            self._application_binding = binding
        if binding is None:
            raise RuntimeProjectionAuthenticationError(
                "Runtime projection credential is invalid"
            )
        if not hmac.compare_digest(str(binding.id), binding_id):
            raise RuntimeProjectionBindingError(
                "Runtime projection binding is stale"
            )
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(expected, api_server_key):
            raise RuntimeProjectionAuthenticationError(
                "Runtime projection credential is invalid"
            )

    async def project_batch(
        self,
        batch: RuntimeProjectionBatch,
    ) -> RuntimeProjectionResult:
        accepted: list[str] = []
        rejected: list[RuntimeProjectionRejection] = []
        for item in batch.items:
            error_code = await self._store_item(batch, item)
            if error_code is None:
                accepted.append(item.projection_id)
            else:
                rejected.append(
                    RuntimeProjectionRejection(
                        projection_id=item.projection_id,
                        error_code=error_code,
                    )
                )
        await self.db.commit()
        return RuntimeProjectionResult(
            accepted_projection_ids=accepted,
            rejected=rejected,
        )

    async def _store_item(
        self,
        batch: RuntimeProjectionBatch,
        item: RuntimeProjectionItem,
    ) -> str | None:
        existing = await self.db.scalar(
            select(RuntimeProjectionReceipt).where(
                RuntimeProjectionReceipt.projection_id == item.projection_id
            )
        )
        if existing is not None:
            return self._duplicate_error(existing, batch, item)

        if item.run_id is not None and item.seq is not None:
            sequence = await self.db.scalar(
                select(RuntimeProjectionReceipt).where(
                    RuntimeProjectionReceipt.runtime_id == batch.runtime_id,
                    RuntimeProjectionReceipt.run_id == item.run_id,
                    RuntimeProjectionReceipt.seq == item.seq,
                )
            )
            if sequence is not None:
                return (
                    None
                    if sequence.projection_id == item.projection_id
                    and sequence.payload_digest == item.payload_digest
                    else "RUN_SEQUENCE_CONFLICT"
                )

        record = RuntimeProjectionReceipt(
            projection_id=item.projection_id,
            runtime_id=batch.runtime_id,
            binding_id=batch.binding_id,
            projection_type=item.projection_type,
            run_id=item.run_id,
            seq=item.seq,
            config_revision=item.config_revision,
            payload_digest=item.payload_digest,
            payload=item.payload,
            occurred_at=item.occurred_at,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(record)
                await self.db.flush()
                if self.materialize:
                    await self._materialize(item)
        except RuntimeProjectionArtifactError:
            await self._refresh_authenticated_binding()
            return "ARTIFACT_EVENT_INVALID"
        except RuntimeProjectionFileError:
            await self._refresh_authenticated_binding()
            return "FILE_EVENT_INVALID"
        except IntegrityError:
            concurrent = await self.db.scalar(
                select(RuntimeProjectionReceipt).where(
                    RuntimeProjectionReceipt.projection_id
                    == item.projection_id
                )
            )
            if concurrent is not None:
                error_code = self._duplicate_error(concurrent, batch, item)
            else:
                error_code = "RUN_SEQUENCE_CONFLICT"
            await self._refresh_authenticated_binding()
            return error_code
        return None

    async def _refresh_authenticated_binding(self) -> None:
        # Rolling back one per-item savepoint expires ORM state across the
        # shared AsyncSession.  The next item must not trigger synchronous
        # lazy loading when it reads counters from the authenticated Binding.
        binding = self._personal_binding or self._application_binding
        if binding is not None:
            await self.db.refresh(binding)

    async def _materialize(self, item: RuntimeProjectionItem) -> None:
        if self._personal_binding is not None:
            await self._materialize_personal(item, self._personal_binding)
            return
        if self._application_binding is not None:
            await self._materialize_application(
                item,
                self._application_binding,
            )
            return
        raise RuntimeProjectionAuthenticationError(
            "Runtime projection was not authenticated"
        )

    async def _materialize_personal(
        self,
        item: RuntimeProjectionItem,
        binding: HermesRuntimeBinding,
    ) -> None:
        payload = item.payload
        if item.projection_type in {"file.ready", "file.failed"}:
            await self._materialize_file_status(item, binding)
            return
        if item.projection_type == "skill.manage.observed":
            await self._materialize_hermes_skill(item, binding)
            return
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return
        run = await self.db.scalar(
            select(RuntimeRun).where(RuntimeRun.runtime_run_id == run_id)
        )
        if item.projection_type == "run.queued":
            source_detail = _public_source_detail(
                payload.get("source_detail")
            )
            request_payload = {
                "message": str(payload.get("prompt") or ""),
                "audit_message": str(payload.get("prompt") or ""),
                "channel": _projection_channel(payload.get("source")),
                "client_message_id": str(
                    payload.get("client_message_id") or ""
                ),
                "config_revision": str(
                    payload.get("config_revision") or ""
                ),
                **(
                    {
                        "source": dict(source_detail),
                        **dict(source_detail),
                    }
                    if isinstance(source_detail, dict)
                    else {}
                ),
            }
            if run is not None:
                if str(run.status or "").lower() == "queued":
                    existing_payload = dict(run.request_payload or {})
                    for key, value in request_payload.items():
                        existing_payload.setdefault(key, value)
                    run.request_payload = existing_payload
                return
            session_id = str(payload.get("session_id") or "")
            session = await self.db.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == int(binding.org_id),
                    RuntimeSession.user_id == int(binding.user_id),
                    RuntimeSession.runtime_workspace_id
                    == int(binding.runtime_workspace_id),
                    RuntimeSession.runtime_session_id == session_id,
                )
            )
            if session is None:
                return
            queued_at = _payload_datetime(
                payload.get("enqueued_at"),
                fallback=item.occurred_at,
            )
            self.db.add(
                RuntimeRun(
                    org_id=int(binding.org_id),
                    user_id=int(binding.user_id),
                    runtime_workspace_id=int(binding.runtime_workspace_id),
                    session_id=int(session.id),
                    runtime_session_id=session_id,
                    runtime_run_id=run_id,
                    model_entry_id=_positive_int(payload.get("model_id")),
                    status="queued",
                    source_type=str(payload.get("source") or "web"),
                    request_payload={
                        **request_payload,
                        "runtime_agent_profile_id": int(
                            session.runtime_agent_profile_id
                        ),
                    },
                    started_at=None,
                )
            )
            session.last_active_at = queued_at
            await self.db.flush()
            return
        if item.projection_type == "run.accepted":
            if run is not None:
                if str(run.status or "").lower() not in {"pending", "queued"}:
                    return
                accepted_at = _payload_datetime(
                    payload.get("accepted_at"),
                    fallback=item.occurred_at,
                )
                run.status = "running"
                run.started_at = accepted_at
                model_id = _positive_int(payload.get("model_id"))
                if run.model_entry_id is None and model_id is not None:
                    run.model_entry_id = model_id
                request_payload = dict(run.request_payload or {})
                request_payload.setdefault(
                    "client_message_id",
                    str(payload.get("client_message_id") or ""),
                )
                request_payload.setdefault(
                    "config_revision",
                    str(payload.get("config_revision") or ""),
                )
                history_count = payload.get(
                    "history_message_count_before_run"
                )
                if isinstance(history_count, int) and history_count >= 0:
                    request_payload["history_message_count_before_run"] = (
                        history_count
                    )
                source_detail = _public_source_detail(
                    payload.get("source_detail")
                )
                if isinstance(source_detail, dict):
                    existing_source = request_payload.get("source")
                    merged_source = dict(source_detail)
                    if isinstance(existing_source, dict):
                        for key in (
                            "im_channel_id",
                            "im_bot_id_digest",
                            "im_target_digest",
                        ):
                            if existing_source.get(key) is not None:
                                merged_source[key] = existing_source[key]
                    request_payload["source"] = merged_source
                    request_payload.update(merged_source)
                run.request_payload = request_payload
                binding.active_run_count = (
                    int(binding.active_run_count or 0) + 1
                )
                return
            session_id = str(payload.get("session_id") or "")
            session = await self.db.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == int(binding.org_id),
                    RuntimeSession.user_id == int(binding.user_id),
                    RuntimeSession.runtime_workspace_id
                    == int(binding.runtime_workspace_id),
                    RuntimeSession.runtime_session_id == session_id,
                )
            )
            if session is None:
                # Projection delivery is FIFO and the Runtime retains its
                # outbox across restarts.  A session can legitimately have
                # been removed before an old Runtime gets a chance to deliver
                # its final projections.  There is no user-owned target left
                # to materialize, so accepting this immutable receipt as a
                # no-op is the only safe outcome.  Raising here makes that
                # one obsolete item block every later Runtime projection.
                return
            accepted_at = _payload_datetime(
                payload.get("accepted_at"),
                fallback=item.occurred_at,
            )
            model_id = _positive_int(payload.get("model_id"))
            run = RuntimeRun(
                org_id=int(binding.org_id),
                user_id=int(binding.user_id),
                runtime_workspace_id=int(binding.runtime_workspace_id),
                session_id=int(session.id),
                runtime_session_id=session_id,
                runtime_run_id=run_id,
                model_entry_id=model_id,
                status="running",
                source_type=str(payload.get("source") or "web"),
                request_payload={
                    "message": str(payload.get("prompt") or ""),
                    "audit_message": str(payload.get("prompt") or ""),
                    "channel": _projection_channel(payload.get("source")),
                    "runtime_agent_profile_id": int(
                        session.runtime_agent_profile_id
                    ),
                    "client_message_id": str(
                        payload.get("client_message_id") or ""
                    ),
                    "config_revision": str(
                        payload.get("config_revision") or ""
                    ),
                    **(
                        {
                            "history_message_count_before_run": payload[
                                "history_message_count_before_run"
                            ]
                        }
                        if isinstance(
                            payload.get("history_message_count_before_run"),
                            int,
                        )
                        and int(payload["history_message_count_before_run"]) >= 0
                        else {}
                    ),
                    **(
                        {
                            "source": dict(source_detail),
                            **dict(source_detail),
                        }
                        if source_detail is not None
                        else {}
                    ),
                },
                started_at=accepted_at,
            )
            self.db.add(run)
            binding.active_run_count = int(binding.active_run_count or 0) + 1
            session.last_active_at = accepted_at
            await self.db.flush()
            return
        if run is None:
            # `run.accepted` is emitted before any run-scoped event.  With the
            # FIFO Runtime outbox, a missing run here therefore denotes an
            # obsolete projection (for example after the associated session
            # was removed), not a retriable ordering race.  Record and accept
            # it so it cannot head-of-line block healthy conversations.
            return
        if item.projection_type == "run.self_evolution.activity":
            await self._materialize_self_evolution_activity(
                item=item,
                run=run,
            )
            return

        if item.projection_type == "run.event":
            seq = int(payload.get("seq") or 0)
            if seq <= 0:
                raise RuntimeError("Runtime event projection has no sequence")
            event_name = str(payload.get("event_type") or "")
            event_data = dict(payload.get("event_data") or {})
            event_data.update(
                {
                    "runtime_workspace_id": int(binding.runtime_workspace_id),
                    "runtime_session_id": str(run.runtime_session_id or ""),
                    "runtime_run_id": run_id,
                }
            )
            self.db.add(
                RuntimeRunEvent(
                    run_id=int(run.id),
                    runtime_run_public_id=run_id,
                    seq=seq,
                    event_name=event_name,
                    dedupe_key=item.projection_id,
                    payload=event_data,
                )
            )
            if event_name in {
                "chat.artifact.created",
                "chat.artifact.updated",
                "chat.artifact.deleted",
            }:
                await self._materialize_artifact_event(
                    binding=binding,
                    run=run,
                    event_name=event_name,
                    event_data=event_data,
                )
            await self.db.flush()
            return
        if item.projection_type == "run.usage":
            await self._materialize_usage(
                item=item,
                org_id=int(binding.org_id),
                user_id=int(binding.user_id),
                runtime_workspace_id=int(binding.runtime_workspace_id),
                session_id=(
                    int(run.session_id)
                    if run.session_id is not None
                    else None
                ),
                run_id=int(run.id),
                runtime_run_id=run_id,
                request_id=str(
                    payload.get("request_id")
                    or (run.request_payload or {}).get("client_message_id")
                    or run_id
                ),
                model_entry_id=run.model_entry_id,
            )
            return
        if item.projection_type == "run.terminal":
            terminal_at = _payload_datetime(
                payload.get("terminal_at"),
                fallback=item.occurred_at,
            )
            status = _terminal_status(payload.get("state"))
            was_active = str(run.status or "").lower() in {
                "accepted",
                "pending",
                "running",
            }
            answer = str(payload.get("answer") or "")
            run.status = status
            run.finished_at = terminal_at
            run.response_summary = {
                **dict(run.response_summary or {}),
                "answer": answer,
                "stream": {
                    "status": status,
                    "terminal_seq": int(payload.get("terminal_seq") or 0),
                },
            }
            if status == "failed":
                run.error_code = str(
                    payload.get("error_code")
                    or payload.get("state")
                    or "runtime_failed"
                )[:100]
            if status == "completed":
                run.self_evolution_status = "eligible"
                run.self_evolution_activation_deadline_at = (
                    terminal_at
                    + timedelta(
                        seconds=(
                            settings.POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS
                        )
                    )
                )
                run.self_evolution_started_at = None
                run.self_evolution_last_activity_at = None
                run.self_evolution_deadline_at = (
                    terminal_at
                    + timedelta(
                        seconds=(
                            settings.POST_TURN_REVIEW_MAX_DURATION_SECONDS
                        )
                    )
                )
                run.self_evolution_error_message = None
            else:
                run.self_evolution_status = None
                run.self_evolution_activation_deadline_at = None
                run.self_evolution_started_at = None
                run.self_evolution_last_activity_at = None
                run.self_evolution_deadline_at = None
                run.self_evolution_error_message = None
            if was_active:
                binding.active_run_count = max(
                    0,
                    int(binding.active_run_count or 0) - 1,
                )
            source = (run.request_payload or {}).get("source")
            if (
                was_active
                and isinstance(source, dict)
                and source.get("type") == "scheduled"
            ):
                task_id = _positive_int(source.get("scheduled_task_id"))
                if task_id is not None:
                    task = await self.db.scalar(
                        select(ScheduledTask).where(
                            ScheduledTask.id == task_id,
                            ScheduledTask.org_id == int(binding.org_id),
                            ScheduledTask.user_id == int(binding.user_id),
                            ScheduledTask.runtime_workspace_id
                            == int(binding.runtime_workspace_id),
                        )
                    )
                    if task is not None:
                        task.last_run_at = terminal_at
                        task.last_run_status = (
                            "success" if status == "completed" else "failed"
                        )
                        task.last_error_code = (
                            None
                            if status == "completed"
                            else str(payload.get("state") or "RUNTIME_FAILED")[:100]
                        )
                        task.last_error = None
                        task.last_runtime_run_id = run_id
                        if status == "completed":
                            task.run_count = int(task.run_count or 0) + 1
                        im_channel_id = _positive_int(
                            source.get("im_channel_id")
                        )
                        if im_channel_id is not None:
                            channel = await self.db.scalar(
                                select(ImChannel).where(
                                    ImChannel.id == im_channel_id,
                                    ImChannel.runtime_session_row_id
                                    == run.session_id,
                                )
                            )
                            target_matches = bool(
                                personal_wecom_channel_is_scheduled_task_eligible(
                                    channel
                                )
                                and channel is not None
                                and opaque_digest(str(channel.bot_id))
                                == source.get("im_bot_id_digest")
                                and channel.proactive_target_digest
                                == source.get("im_target_digest")
                            )
                            if target_matches and channel is not None:
                                delivery_key = (
                                    f"scheduled:{int(task.id)}:{run_id}"
                                )
                                existing_delivery = await self.db.scalar(
                                    select(ImOutboundDelivery.id).where(
                                        ImOutboundDelivery.delivery_key
                                        == delivery_key
                                    )
                                )
                                if existing_delivery is None:
                                    artifact_ids = list(
                                        (
                                            await self.db.execute(
                                                select(ChatFileArtifact.id)
                                                .where(
                                                    ChatFileArtifact.run_id
                                                    == int(run.id),
                                                    ChatFileArtifact.status
                                                    == "active",
                                                )
                                                .order_by(ChatFileArtifact.id)
                                            )
                                        ).scalars()
                                    )
                                    self.db.add(
                                        ImOutboundDelivery(
                                            delivery_key=delivery_key,
                                            channel_id=int(channel.id),
                                            scheduled_task_id=int(task.id),
                                            runtime_run_row_id=int(run.id),
                                            runtime_run_id=run_id,
                                            bot_id_digest=str(
                                                source["im_bot_id_digest"]
                                            ),
                                            target_encrypted=str(
                                                channel.proactive_target_encrypted
                                            ),
                                            target_digest=str(
                                                channel.proactive_target_digest
                                            ),
                                            content=_scheduled_wecom_delivery_content(
                                                request_payload=(
                                                    run.request_payload or {}
                                                ),
                                                source=source,
                                                result=(
                                                    answer
                                                    if status == "completed"
                                                    else "定时任务执行失败，请在 Web 端查看详情。"
                                                ),
                                            ),
                                            artifact_ids=artifact_ids,
                                            delivered_artifact_ids=[],
                                            status="pending",
                                            next_attempt_at=terminal_at,
                                        )
                                    )
                            else:
                                task.last_run_status = "failed"
                                task.last_error_code = (
                                    "WECOM_DELIVERY_TARGET_CHANGED"
                                )
                                task.last_error = None
            session = await self.db.scalar(
                select(RuntimeSession).where(RuntimeSession.id == run.session_id)
            )
            if session is not None:
                session.last_active_at = terminal_at
                session.message_count_cache = (
                    int(session.message_count_cache or 0) + 2
                )
            await self.db.flush()

    async def _materialize_hermes_skill(
        self, item: RuntimeProjectionItem, binding: HermesRuntimeBinding
    ) -> None:
        payload = item.payload
        slug = str(payload.get("slug") or payload.get("skill_name") or "").strip().lower()
        name = str(payload.get("skill_name") or "").strip()
        action = str(payload.get("action") or "")
        if not slug or not name or action not in {"create", "update", "delete"}:
            return
        skill = await self.db.scalar(select(Skill).where(
            Skill.org_id == binding.org_id,
            Skill.owner_user_id == binding.user_id,
            Skill.slug == slug,
        ))
        if action == "delete":
            if skill is not None:
                skill.status = "deleted"
                skill.market_visible = False
                skill.archived_at = datetime.now(timezone.utc)
            return
        if skill is not None and skill.status == "deleted":
            return
        if skill is None:
            skill = Skill(
                org_id=binding.org_id, owner_user_id=binding.user_id, slug=slug,
                name=name, description=payload.get("description") if isinstance(payload.get("description"), str) else None,
                category_key="other", asset_scope="personal", visibility="private",
                status="private", market_visible=False,
            )
            self.db.add(skill)
            await self.db.flush()
        else:
            skill.name = name
            if isinstance(payload.get("description"), str):
                skill.description = payload["description"]
        latest = await self.db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill.id).order_by(SkillVersion.version.desc()))
        version = SkillVersion(
            org_id=binding.org_id, skill_id=skill.id,
            version=int(latest.version if latest else 0) + 1,
            package_uri=None,
            package_sha256=payload.get("content_sha256") if isinstance(payload.get("content_sha256"), str) else None,
            manifest={"kind": "hermes_managed", "summary": payload.get("summary"), "runtime_id": binding.runtime_id},
            status="private",
        )
        self.db.add(version)
        await self.db.flush()
        skill.current_version_id = version.id

    async def _materialize_artifact_event(
        self,
        *,
        binding: HermesRuntimeBinding,
        run: RuntimeRun,
        event_name: str,
        event_data: dict[str, object],
    ) -> None:
        """Upsert one observer event without escaping this projection transaction."""

        if (
            int(run.org_id) != int(binding.org_id)
            or int(run.user_id) != int(binding.user_id)
            or int(run.runtime_workspace_id)
            != int(binding.runtime_workspace_id)
        ):
            raise RuntimeProjectionArtifactError("artifact run binding mismatch")
        relative_path = event_data.get("relative_path")
        if not isinstance(relative_path, str):
            raise RuntimeProjectionArtifactError("artifact relative path is missing")
        row = await self.db.execute(
            select(RuntimeSession, RuntimeAgentProfile)
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.id
                == RuntimeSession.runtime_agent_profile_id,
            )
            .where(
                RuntimeSession.id == run.session_id,
                RuntimeSession.org_id == int(binding.org_id),
                RuntimeSession.user_id == int(binding.user_id),
                RuntimeSession.runtime_workspace_id
                == int(binding.runtime_workspace_id),
            )
        )
        result = row.first()
        if result is None:
            raise RuntimeProjectionArtifactError("artifact session is unavailable")
        session, profile = result
        try:
            normalized = normalize_workspace_relative_path(relative_path)
            root = session_workspace_relative_root(
                str(session.runtime_session_id),
                runtime_agent_profile_id=int(profile.id),
                hermes_profile_name=str(profile.hermes_profile_name),
            )
            scoped = session_scoped_relative_path(normalized, root)
            # session_scoped_relative_path permits bare file names for user API
            # ergonomics; Runtime observer events must already be fully scoped.
            if scoped != normalized:
                raise RuntimeProjectionArtifactError("artifact escapes session root")
        except ChatFileError as exc:
            raise RuntimeProjectionArtifactError("artifact path is invalid") from exc
        matches = (
            await self.db.execute(
                select(ChatFileArtifact)
                .where(
                    ChatFileArtifact.org_id == int(binding.org_id),
                    ChatFileArtifact.user_id == int(binding.user_id),
                    ChatFileArtifact.runtime_workspace_id
                    == int(binding.runtime_workspace_id),
                    ChatFileArtifact.session_id == int(session.id),
                    ChatFileArtifact.relative_path == normalized,
                    ChatFileArtifact.status == "active",
                )
                .order_by(ChatFileArtifact.id.desc())
            )
        ).scalars().all()
        if event_name == "chat.artifact.deleted":
            for artifact in matches:
                artifact.status = "deleted"
            return
        size = event_data.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeProjectionArtifactError("artifact size is invalid")
        mime_type = event_data.get("mime_type")
        if mime_type is not None and not isinstance(mime_type, str):
            raise RuntimeProjectionArtifactError("artifact mime type is invalid")
        if matches:
            artifact = matches[0]
            artifact.run_id = int(run.id)
            artifact.mime_type = mime_type
            artifact.size_bytes = size
            artifact.source_type = "runtime"
            artifact.visibility = "owner"
            for duplicate in matches[1:]:
                duplicate.status = "deleted"
            return
        self.db.add(
            ChatFileArtifact(
                org_id=int(binding.org_id),
                user_id=int(binding.user_id),
                runtime_workspace_id=int(binding.runtime_workspace_id),
                session_id=int(session.id),
                run_id=int(run.id),
                relative_path=normalized,
                mime_type=mime_type,
                size_bytes=size,
                source_type="runtime",
                visibility="owner",
                status="active",
            )
        )

    async def _materialize_self_evolution_activity(
        self,
        *,
        item: RuntimeProjectionItem,
        run: RuntimeRun,
    ) -> None:
        if str(run.status or "") != "completed":
            raise RuntimeError(
                "Post-turn review activity requires a completed Run"
            )
        now = _payload_datetime(
            item.payload.get("observed_at"),
            fallback=item.occurred_at,
        )
        activation_deadline = _as_utc(
            run.self_evolution_activation_deadline_at
        )
        absolute_deadline = _as_utc(run.self_evolution_deadline_at)
        status = str(run.self_evolution_status or "")
        allowed = (
            absolute_deadline is not None
            and now <= absolute_deadline
            and (
                status == "running"
                or (
                    status == "eligible"
                    and activation_deadline is not None
                    and now <= activation_deadline
                )
            )
        )
        if not allowed:
            return
        if status == "eligible":
            run.self_evolution_status = "running"
            run.self_evolution_started_at = now
            run.self_evolution_error_message = None
            latest_seq = (
                await self.db.scalar(
                    select(RuntimeRunEvent.seq)
                    .where(RuntimeRunEvent.run_id == int(run.id))
                    .order_by(RuntimeRunEvent.seq.desc())
                    .limit(1)
                )
            ) or 0
            self.db.add(
                RuntimeRunEvent(
                    run_id=int(run.id),
                    runtime_run_public_id=str(run.runtime_run_id),
                    seq=int(latest_seq) + 1,
                    event_name="chat.self_evolution.event",
                    dedupe_key=item.projection_id,
                    payload={
                        "status": "running",
                        "activity": (
                            "已启用本次执行复盘，正在优化相关技能"
                        ),
                    },
                )
            )
        run.self_evolution_last_activity_at = now
        await self.db.flush()

    async def _materialize_file_status(
        self,
        item: RuntimeProjectionItem,
        binding: HermesRuntimeBinding,
    ) -> None:
        payload = item.payload
        file_id = _positive_int(payload.get("file_id"))
        relative_path = str(payload.get("relative_path") or "")
        session_runtime_id = str(payload.get("session_id") or "")
        if (
            file_id is None
            or not relative_path
            or not session_runtime_id
        ):
            raise RuntimeProjectionFileError(
                "Runtime file projection is invalid"
            )
        artifact = await self.db.scalar(
            select(ChatFileArtifact).where(
                ChatFileArtifact.id == file_id,
                ChatFileArtifact.org_id == int(binding.org_id),
                ChatFileArtifact.user_id == int(binding.user_id),
                ChatFileArtifact.runtime_workspace_id
                == int(binding.runtime_workspace_id),
            )
        )
        if artifact is None:
            raise RuntimeProjectionFileError(
                "Runtime file metadata is unavailable"
            )
        session = await self.db.scalar(
            select(RuntimeSession).where(
                RuntimeSession.id == artifact.session_id,
                RuntimeSession.runtime_session_id == session_runtime_id,
            )
        )
        if (
            session is None
            or str(artifact.relative_path) != relative_path
            or str(artifact.status) not in {"pending", "active", "failed"}
        ):
            raise RuntimeProjectionFileError(
                "Runtime file projection does not match intent"
            )
        if item.projection_type == "file.failed":
            if str(artifact.status) == "active":
                return
            artifact.status = "failed"
            await self.db.flush()
            return
        size_bytes = _positive_int(payload.get("size_bytes"))
        sha256_digest = str(payload.get("sha256") or "")
        if size_bytes is None or len(sha256_digest) != 64:
            raise RuntimeProjectionFileError(
                "Runtime file projection is invalid"
            )
        if str(artifact.status) == "active":
            if (
                int(artifact.size_bytes) != size_bytes
                or str(artifact.sha256_digest or "") != sha256_digest
            ):
                raise RuntimeProjectionFileError(
                    "Runtime file projection conflicts"
                )
            return
        artifact.size_bytes = size_bytes
        artifact.sha256_digest = sha256_digest
        artifact.mime_type = (
            str(payload.get("mime_type"))
            if payload.get("mime_type")
            else artifact.mime_type
        )
        artifact.status = "active"
        await self.db.flush()

    async def _materialize_application(
        self,
        item: RuntimeProjectionItem,
        binding: ApplicationAgentRuntimeBinding,
    ) -> None:
        payload = item.payload
        if item.projection_type in {"file.ready", "file.failed"}:
            try:
                await ApplicationAgentStudioChatFileService(
                    self.db
                ).apply_file_status(
                    binding=binding,
                    projection_type=item.projection_type,
                    payload=payload,
                )
            except (ChatFileError, ChatFilePermissionError) as exc:
                raise RuntimeProjectionFileError(str(exc)) from exc
            return
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return
        run = await self.db.scalar(
            select(ApplicationAgentRun).where(
                ApplicationAgentRun.runtime_run_id == run_id
            )
        )
        if item.projection_type == "run.queued":
            if run is not None:
                return
            enqueued_at = _payload_datetime(
                payload.get("enqueued_at"),
                fallback=item.occurred_at,
            )
            session_id = str(payload.get("session_id") or "")
            run = ApplicationAgentRun(
                org_id=int(binding.org_id),
                application_agent_id=int(binding.application_agent_id),
                runtime_binding_id=int(binding.id),
                application_client_id=None,
                conversation_id=None,
                runtime_session_id=session_id,
                active_session_key=session_id,
                runtime_run_id=run_id,
                turn_id=None,
                subject_type="user",
                subject_user_id=_positive_int(payload.get("user_id")),
                subject_service_id=None,
                channel=_application_projection_channel(
                    payload.get("source")
                ),
                status="queued",
                request_id=str(payload.get("client_message_id") or run_id),
                idempotency_key=None,
                submission_state="reserved",
                submit_intent_at=enqueued_at,
                started_at=enqueued_at,
            )
            self.db.add(run)
            binding.queued_run_count = int(binding.queued_run_count or 0) + 1
            await self._touch_studio_session(
                binding=binding,
                payload=payload,
                occurred_at=enqueued_at,
            )
            await self.db.flush()
            return
        if item.projection_type == "run.accepted":
            if run is not None:
                if (
                    str(run.status) == "queued"
                    and run.capacity_released_at is None
                ):
                    binding.queued_run_count = max(
                        0,
                        int(binding.queued_run_count or 0) - 1,
                    )
                    binding.active_run_count = (
                        int(binding.active_run_count or 0) + 1
                    )
                    run.status = "running"
                run.submission_state = "accepted"
                run.dashboard_accepted_at = run.dashboard_accepted_at or (
                    _payload_datetime(
                        payload.get("accepted_at"),
                        fallback=item.occurred_at,
                    )
                )
                await self._touch_studio_session(
                    binding=binding,
                    payload=payload,
                    occurred_at=item.occurred_at,
                )
                await self.db.flush()
                return
            accepted_at = _payload_datetime(
                payload.get("accepted_at"),
                fallback=item.occurred_at,
            )
            user_id = _positive_int(payload.get("user_id"))
            run = ApplicationAgentRun(
                org_id=int(binding.org_id),
                application_agent_id=int(binding.application_agent_id),
                runtime_binding_id=int(binding.id),
                application_client_id=None,
                conversation_id=None,
                runtime_session_id=str(payload.get("session_id") or ""),
                active_session_key=str(payload.get("session_id") or ""),
                runtime_run_id=run_id,
                turn_id=None,
                subject_type="user",
                subject_user_id=user_id,
                subject_service_id=None,
                channel=_application_projection_channel(
                    payload.get("source")
                ),
                status="running",
                request_id=str(
                    payload.get("client_message_id") or run_id
                ),
                idempotency_key=None,
                submission_state="accepted",
                submit_intent_at=accepted_at,
                dashboard_accepted_at=accepted_at,
                started_at=accepted_at,
            )
            self.db.add(run)
            binding.active_run_count = int(binding.active_run_count or 0) + 1
            await self._touch_studio_session(
                binding=binding,
                payload=payload,
                occurred_at=accepted_at,
            )
            await self.db.flush()
            return
        if run is None:
            raise RuntimeError(
                "Application Runtime projection Run is not available yet"
            )
        if item.projection_type == "run.event":
            seq = int(payload.get("seq") or 0)
            if seq <= 0:
                raise RuntimeError("Runtime event projection has no sequence")
            event_name = str(payload.get("event_type") or "")
            event_data = dict(payload.get("event_data") or {})
            if event_name in {
                "chat.artifact.created",
                "chat.artifact.updated",
                "chat.artifact.deleted",
            }:
                try:
                    await ApplicationAgentStudioChatFileService(
                        self.db
                    ).materialize_artifact_event(
                        binding=binding,
                        run=run,
                        event_name=event_name,
                        event_data=event_data,
                    )
                except (ChatFileError, ChatFilePermissionError) as exc:
                    raise RuntimeProjectionArtifactError(str(exc)) from exc
            await self.db.flush()
            return
        if item.projection_type == "run.usage":
            agent = await self.db.scalar(
                select(ApplicationAgent).where(
                    ApplicationAgent.id == int(binding.application_agent_id)
                )
            )
            model_entry_id = _positive_int(
                (agent.config or {}).get("model_entry_id")
                if agent is not None
                else None
            )
            await self._materialize_usage(
                item=item,
                org_id=int(binding.org_id),
                user_id=int(run.subject_user_id or 0),
                runtime_workspace_id=int(binding.application_agent_id),
                session_id=None,
                run_id=int(run.id),
                runtime_run_id=run_id,
                request_id=str(
                    payload.get("request_id") or run.request_id
                ),
                model_entry_id=model_entry_id,
            )
            return
        if item.projection_type == "run.terminal":
            previous_status = str(run.status)
            run.status = _terminal_status(payload.get("state"))
            run.finished_at = _payload_datetime(
                payload.get("terminal_at"),
                fallback=item.occurred_at,
            )
            run.error_code = (
                str(
                    payload.get("error_code")
                    or payload.get("state")
                    or ""
                )[:100]
                if run.status == "failed"
                else None
            )
            run.active_session_key = None
            if run.capacity_released_at is None:
                run.capacity_released_at = run.finished_at
                if previous_status == "queued":
                    binding.queued_run_count = max(
                        0,
                        int(binding.queued_run_count or 0) - 1,
                    )
                else:
                    binding.active_run_count = max(
                        0,
                        int(binding.active_run_count or 0) - 1,
                    )
            studio_session = await self.db.scalar(
                select(ApplicationAgentStudioSession).where(
                    ApplicationAgentStudioSession.application_agent_id
                    == int(binding.application_agent_id),
                    ApplicationAgentStudioSession.runtime_session_id
                    == str(run.runtime_session_id),
                    ApplicationAgentStudioSession.status == "active",
                )
            )
            if studio_session is not None:
                studio_session.last_active_at = run.finished_at
            await self.db.flush()

    async def _materialize_usage(
        self,
        *,
        item: RuntimeProjectionItem,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        session_id: int | None,
        run_id: int,
        runtime_run_id: str,
        request_id: str,
        model_entry_id: int | None,
    ) -> None:
        usage = dict(item.payload.get("usage") or {})
        model = (
            await self.db.scalar(
                select(ModelEntry).where(ModelEntry.id == model_entry_id)
            )
            if model_entry_id is not None
            else None
        )
        self.db.add(
            ModelUsageLog(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                session_id=session_id,
                run_id=run_id,
                runtime_run_id=runtime_run_id,
                request_id=request_id[:128],
                model_entry_id=model_entry_id,
                provider_id=(
                    int(model.provider_id)
                    if model is not None
                    else None
                ),
                input_tokens=_nonnegative_int(
                    usage.get("prompt_tokens", usage.get("input_tokens"))
                ),
                output_tokens=_nonnegative_int(
                    usage.get(
                        "completion_tokens",
                        usage.get("output_tokens"),
                    )
                ),
                latency_ms=_nonnegative_int(usage.get("latency_ms")),
                status="success",
                error_code=None,
            )
        )
        await self.db.flush()

    async def _touch_studio_session(
        self,
        *,
        binding: ApplicationAgentRuntimeBinding,
        payload: dict,
        occurred_at: datetime,
    ) -> None:
        if str(payload.get("source") or "") != "web":
            return
        session_id = str(payload.get("session_id") or "")
        user_id = _positive_int(payload.get("user_id"))
        if not session_id or user_id is None:
            return
        session = await self.db.scalar(
            select(ApplicationAgentStudioSession).where(
                ApplicationAgentStudioSession.application_agent_id
                == int(binding.application_agent_id),
                ApplicationAgentStudioSession.user_id == user_id,
                ApplicationAgentStudioSession.runtime_session_id
                == session_id,
            )
        )
        if session is None:
            self.db.add(
                ApplicationAgentStudioSession(
                    org_id=int(binding.org_id),
                    application_agent_id=int(
                        binding.application_agent_id
                    ),
                    user_id=user_id,
                    runtime_session_id=session_id,
                    title="新任务",
                    status="active",
                    last_active_at=occurred_at,
                )
            )
            return
        if session.status == "active":
            session.last_active_at = occurred_at

    @staticmethod
    def _duplicate_error(
        existing: RuntimeProjectionReceipt,
        batch: RuntimeProjectionBatch,
        item: RuntimeProjectionItem,
    ) -> str | None:
        if (
            existing.runtime_id == batch.runtime_id
            and existing.binding_id == batch.binding_id
            and existing.projection_type == item.projection_type
            and existing.run_id == item.run_id
            and existing.seq == item.seq
            and existing.payload_digest == item.payload_digest
        ):
            return None
        return "PROJECTION_ID_CONFLICT"


async def delete_expired_runtime_projection_events(
    db: AsyncSession,
    *,
    older_than: datetime,
) -> int:
    """Prune projected trace events after their durable chat result exists."""

    result = await db.execute(
        delete(RuntimeRunEvent)
        .where(RuntimeRunEvent.created_at < older_than)
        .where(
            RuntimeRunEvent.run_id.in_(
                select(RuntimeRun.id).where(
                    or_(
                        RuntimeRun.status.in_(
                            ("failed", "canceled", "cancel_failed")
                        ),
                        (
                            (RuntimeRun.status == "completed")
                            & RuntimeRun.chat_log_projected_at.is_not(None)
                        ),
                    )
                )
            )
        )
    )
    await db.commit()
    return int(result.rowcount or 0)


def _payload_datetime(value: object, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return fallback if fallback.tzinfo is not None else fallback.replace(
        tzinfo=timezone.utc
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int(value: object) -> int | None:
    try:
        result = int(str(value))
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _nonnegative_int(value: object) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _terminal_status(value: object) -> str:
    state = str(value or "").upper()
    if state == "COMPLETED":
        return "completed"
    if state == "CANCELED":
        return "canceled"
    if state == "PARKED_FOR_CLARIFY":
        return "parked_for_clarify"
    return "failed"


def _scheduled_wecom_delivery_content(
    *,
    request_payload: object,
    source: dict[str, object],
    result: str,
) -> str:
    payload = request_payload if isinstance(request_payload, dict) else {}
    raw_task_name = source.get("scheduled_task_name")
    task_name = (
        raw_task_name.strip()
        if isinstance(raw_task_name, str) and raw_task_name.strip()
        else "未命名定时任务"
    )
    raw_prompt = payload.get("message")
    prompt = (
        raw_prompt
        if isinstance(raw_prompt, str) and raw_prompt.strip()
        else "未提供任务内容"
    )
    delivery_result = result or "定时任务已执行完成。"
    return (
        "### 定时任务执行结果\n\n"
        f"**任务名称：** {task_name}\n\n"
        "**任务内容：**\n"
        f"{prompt}\n\n"
        "### 执行结果\n\n"
        f"{delivery_result}"
    )


def _projection_channel(value: object) -> str:
    source = str(value or "web")
    if source in {"personal_wecom", "application_wecom"}:
        return "wecom"
    if source == "scheduled_task":
        return "scheduled_task"
    if source == "openapi":
        return "openapi"
    return "web"


def _application_projection_channel(value: object) -> str:
    if str(value or "web") == "web":
        return "studio"
    return _projection_channel(value)
