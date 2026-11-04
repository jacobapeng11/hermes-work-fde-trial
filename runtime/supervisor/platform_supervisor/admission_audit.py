from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

import httpx


logger = logging.getLogger(__name__)

_ALLOWED_CONTEXT_FIELDS = frozenset(
    {
        "session_id",
        "request_id",
        "client_message_id",
        "run_id",
        "error_code",
        "exception_type",
        "profile_id",
        "model_id",
        "config_revision",
        "attachment_count",
        "prompt_bytes",
        "upstream_status",
        "upstream_path",
    }
)


class AdmissionAuditReporter:
    """Best-effort admission diagnostics; it must never affect Edge replies."""

    def __init__(
        self,
        *,
        backend_base_url: str,
        runtime_id: str,
        binding_id: str,
        api_server_key: str,
        client: httpx.AsyncClient | None = None,
        queue_size: int = 2048,
        batch_size: int = 50,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
    ) -> None:
        self.backend_base_url = backend_base_url.rstrip("/")
        self.runtime_id = runtime_id
        self.binding_id = binding_id
        self.api_server_key = api_server_key
        self._client = client
        self._owns_client = client is None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._batch_size = max(1, min(50, int(batch_size)))
        self._max_attempts = max(1, int(max_attempts))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._worker: asyncio.Task[None] | None = None

    def record(
        self,
        *,
        operation: str,
        status: str,
        outcome: str,
        stage: str,
        **context: Any,
    ) -> str:
        event_id = str(uuid4())
        event = {
            "event_id": event_id,
            "runtime_id": self.runtime_id,
            "operation": operation,
            "status": status,
            "outcome": outcome,
            "stage": stage,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            **{
                key: value
                for key, value in context.items()
                if key in _ALLOWED_CONTEXT_FIELDS and value is not None
            },
        }
        logger.info("runtime_admission_audit", extra={"event": "runtime_admission_audit", **event})
        self._ensure_worker()
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "runtime_admission_audit_dropped event_id=%s error_type=QueueFull",
                event_id,
                extra={
                    "event": "runtime_admission_audit_dropped",
                    "runtime_id": self.runtime_id,
                    "event_id": event_id,
                    "error_type": "QueueFull",
                },
            )
        return event_id

    async def flush(self) -> None:
        """Wait for currently queued best-effort events without blocking record()."""

        if self._worker is not None:
            await self._queue.join()

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run_worker(),
                name=f"runtime-admission-audit-worker:{self.runtime_id}",
            )

    async def _run_worker(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._report_batch(batch)
            finally:
                for _event in batch:
                    self._queue.task_done()

    async def _report_batch(self, events: list[dict[str, Any]]) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = await self._shared_client().post(
                    f"{self.backend_base_url}/internal/v1/runtime-admission-audits",
                    json={
                        "schema_version": 1,
                        "runtime_id": self.runtime_id,
                        "binding_id": self.binding_id,
                        "items": events,
                    },
                    headers={"Authorization": f"Bearer {self.api_server_key}"},
                )
                if self._permanent_status(response.status_code):
                    response.raise_for_status()
                response.raise_for_status()
                self._validate_acknowledgement(response, events)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if not self._retryable(exc, response):
                    break
                if attempt >= self._max_attempts:
                    break
                if self._retry_backoff_seconds:
                    await asyncio.sleep(
                        self._retry_backoff_seconds * (2 ** (attempt - 1))
                    )
        assert last_error is not None
        first_event_id = str(events[0]["event_id"])
        logger.warning(
            "runtime_admission_audit_report_failed event_id=%s error_type=%s batch_size=%s",
            first_event_id,
            type(last_error).__name__,
            len(events),
            extra={
                "event": "runtime_admission_audit_report_failed",
                "runtime_id": self.runtime_id,
                "event_id": first_event_id,
                "error_type": type(last_error).__name__,
                "batch_size": len(events),
            },
        )

    @staticmethod
    def _validate_acknowledgement(
        response: httpx.Response,
        events: list[dict[str, Any]],
    ) -> None:
        acknowledgement = response.json()
        accepted = set(acknowledgement.get("accepted_event_ids") or [])
        duplicates = set(acknowledgement.get("duplicate_event_ids") or [])
        rejected = set(acknowledgement.get("rejected_event_ids") or [])
        expected = {str(event["event_id"]) for event in events}
        if rejected & expected:
            raise RuntimeError("Runtime admission audit was rejected")
        if expected != (accepted | duplicates) & expected:
            raise RuntimeError("Runtime admission audit acknowledgement is incomplete")

    @staticmethod
    def _permanent_status(status_code: int) -> bool:
        return 400 <= status_code < 500 and status_code not in {408, 425, 429}

    @classmethod
    def _retryable(
        cls,
        exc: Exception,
        response: httpx.Response | None,
    ) -> bool:
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if response is not None:
            return not cls._permanent_status(response.status_code)
        return isinstance(exc, (RuntimeError, ValueError))

    def _shared_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(connect=0.5, pool=0.1, write=0.5, read=1.0),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client
