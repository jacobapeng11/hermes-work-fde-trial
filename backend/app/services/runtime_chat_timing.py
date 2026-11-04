from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from string import ascii_letters, digits


_ORDERED_STAGES = (
    "request_received",
    "backend_request_received",
    "provider_received",
    "db_committed",
    "identity_resolved",
    "scheduler_claimed",
    "runtime_activate_started",
    "prompt_build_started",
    "runtime_activate_done",
    "prompt_build_done",
    "web_queue_admission_done",
    "attachments_done",
    "ready_receipt_reserve_done",
    "ready_receipt_prepare_done",
    "run_prepare_done",
    "run_profile_gate_done",
    "run_reserved_id_precheck_done",
    "run_binding_lock_done",
    "run_ready_identity_done",
    "run_profile_lock_done",
    "run_session_lock_done",
    "run_ready_target_done",
    "run_reserved_id_check_done",
    "run_active_check_done",
    "run_insert_begin",
    "run_insert_done",
    "run_commit_begin",
    "run_commit_done",
    "agent_lookup_done",
    "runtime_ensure_done",
    "run_created",
    "transport_control_done",
    "admission_done",
    "prepared_state_done",
    "history_checkpoint_done",
    "history_done",
    "source_lookup_done",
    "request_sessions_closed",
    "stream_ready",
    "sse_stream_ready",
    "broker_subscribed",
    "background_launched",
    "accepted_sse_yield",
    "first_sse_yield",
    "executor_started",
    "projector_loaded",
    "invocation_authorized",
    "application_authorized",
    "conversation_resolved",
    "session_materialization_started",
    "session_materialized",
    "session_prewarm_started",
    "session_prewarm_completed",
    "run_committed",
    "im_run_bound",
    "capacity_acquired",
    "running_committed",
    "prepared_context_ready",
    "model_route_prepare_started",
    "model_route_prepare_done",
    "skill_observer_done",
    "session_ensure_done",
    "session_import_done",
    "attachment_staging_done",
    "artifact_baseline_done",
    "prompt_submitted",
    "dashboard_stream_begin",
    "runtime_submitted",
    "hot_channel_acquired",
    "dashboard_connection_acquired",
    "workspace_ready",
    "session_resumed",
    "model_ready",
    "cwd_ready",
    "prompt_submit_begin",
    "prompt_ack",
    "first_stream_event",
    "first_agent_event",
    "first_visible_event_received",
    "first_runtime_public_event",
    "first_visible_persisted",
    "first_visible_published",
    "first_provider_dispatch",
    "first_provider_confirmed",
)
_SAFE_METADATA_CHARACTERS = frozenset(
    ascii_letters + digits + "._:-",
)


@dataclass
class RuntimeChatTiming:
    """Process-local, non-persistent timing marks for one Runtime Run."""

    clock: Callable[[], float] = time.perf_counter
    runtime_run_id: str | None = None
    _marks: dict[str, float] = field(default_factory=dict)
    _metadata: dict[str, str] = field(default_factory=dict)

    def mark(self, name: str) -> bool:
        if name not in _ORDERED_STAGES or name in self._marks:
            return False
        self._marks[name] = self.clock()
        return True

    def bind_run(self, runtime_run_id: str) -> None:
        candidate = str(runtime_run_id)
        if self.runtime_run_id is None:
            self.runtime_run_id = candidate
        elif self.runtime_run_id != candidate:
            raise ValueError("runtime timing cannot be rebound to another run")

    def set_metadata(self, name: str, value: str) -> None:
        safe_name = str(name)
        safe_value = str(value)
        if (
            not safe_name
            or len(safe_name) > 64
            or len(safe_value) > 128
            or any(
                character not in _SAFE_METADATA_CHARACTERS
                for character in safe_name
            )
            or any(
                character not in _SAFE_METADATA_CHARACTERS
                for character in safe_value
            )
        ):
            return
        self._metadata[safe_name] = safe_value

    def duration_ms(self, start: str, end: str) -> float | None:
        if start not in self._marks or end not in self._marks:
            return None
        return round(max(0.0, self._marks[end] - self._marks[start]) * 1000, 3)

    def safe_snapshot(
        self,
        *,
        duration_pairs: Iterable[tuple[str, str]] | None = None,
    ) -> dict[str, object]:
        durations: dict[str, float] = {}
        if duration_pairs is None:
            present = [name for name in _ORDERED_STAGES if name in self._marks]
            for start, end in zip(present, present[1:]):
                value = self.duration_ms(start, end)
                if value is not None:
                    durations[f"{start}_to_{end}"] = value
            total = self.duration_ms(
                "request_received",
                "first_visible_published",
            )
            if total is not None:
                durations["request_received_to_first_visible_published"] = total
        else:
            for start, end in duration_pairs:
                start_value = self._marks.get(start)
                end_value = self._marks.get(end)
                if (
                    start_value is None
                    or end_value is None
                    or end_value < start_value
                ):
                    continue
                durations[f"{start}_to_{end}"] = round(
                    (end_value - start_value) * 1000,
                    3,
                )
        return {
            "runtime_run_id": self.runtime_run_id,
            "durations_ms": durations,
            "metadata": dict(self._metadata),
        }
