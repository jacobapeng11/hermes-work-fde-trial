from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

import httpx

from platform_supervisor.contracts import (
    RuntimeProjectionAck,
    canonical_payload_digest,
)
from platform_supervisor.settings import SupervisorSettings
from platform_supervisor.storage import SQLiteStore, StoredProjection


logger = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE_LENGTH = 240


class ProjectionSender:
    """Best-effort HTTP delivery backed exclusively by the durable SQLite Outbox."""

    def __init__(
        self,
        settings: SupervisorSettings,
        store: SQLiteStore,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self._client = client
        self._owns_client = client is None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="runtime-projection-sender",
            )

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._reset_owned_client()

    async def deliver_once(self) -> int:
        batch = await self.store.load_projection_batch(
            max_items=100,
            max_bytes=256 * 1024,
        )
        if not batch:
            return 0
        response = await self._shared_client().post(
            self._endpoint(),
            json=self._batch_payload(batch),
            headers={
                "Authorization": (
                    f"Bearer {self.settings.API_SERVER_KEY.get_secret_value()}"
                )
            },
        )
        response.raise_for_status()
        ack = RuntimeProjectionAck.model_validate(response.json())
        if ack.runtime_id != self.settings.RUNTIME_ID:
            raise RuntimeError("projection ACK runtime_id mismatch")
        sent_ids = {item.projection_id for item in batch}
        accepted = [
            projection_id
            for projection_id in ack.accepted_projection_ids
            if projection_id in sent_ids
        ]
        rejected_ids: list[str] = []
        if accepted:
            await self.store.acknowledge_projections(
                accepted,
                acknowledged_at=datetime.now(timezone.utc),
            )
        if ack.rejected:
            logger.error(
                "Backend rejected Runtime projections",
                extra={
                    "component": "projection_sender",
                    "runtime_id": self.settings.RUNTIME_ID,
                    "rejected_count": len(ack.rejected),
                },
            )
            # A Backend semantic rejection is not a transport failure. Leaving
            # it at the FIFO head retries the exact same invalid path forever
            # and prevents later Run events from reaching the index.
            rejected_ids = [
                item.projection_id
                for item in ack.rejected
                if item.projection_id in sent_ids
            ]
            if rejected_ids:
                await self.store.acknowledge_projections(
                    rejected_ids,
                    acknowledged_at=datetime.now(timezone.utc),
                )
        return len(accepted) + len(rejected_ids)

    async def _run(self) -> None:
        backoff = self.settings.PROJECTION_POLL_INTERVAL_SECONDS
        consecutive_failures = 0
        while not self._closed:
            try:
                delivered = await self.deliver_once()
                backoff = self.settings.PROJECTION_POLL_INTERVAL_SECONDS
                consecutive_failures = 0
                if delivered == 0:
                    await asyncio.sleep(
                        self.settings.PROJECTION_POLL_INTERVAL_SECONDS
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_failures += 1
                retry_in_seconds = backoff
                client_reset = False
                if isinstance(exc, httpx.TransportError):
                    client_reset = await self._reset_owned_client()
                error_type = type(exc).__name__
                error_message = self._safe_error_message(exc)
                logger.warning(
                    "Runtime projection delivery failed "
                    "error_type=%s consecutive_failures=%d "
                    "retry_in_seconds=%.3f client_reset=%s error=%s",
                    error_type,
                    consecutive_failures,
                    retry_in_seconds,
                    str(client_reset).lower(),
                    error_message,
                    extra={
                        "component": "projection_sender",
                        "runtime_id": self.settings.RUNTIME_ID,
                        "error_type": error_type,
                        "error_message": error_message,
                        "consecutive_failures": consecutive_failures,
                        "retry_in_seconds": retry_in_seconds,
                        "client_reset": client_reset,
                    },
                )
                await asyncio.sleep(retry_in_seconds)
                backoff = min(
                    self.settings.PROJECTION_MAX_BACKOFF_SECONDS,
                    max(1.0, backoff * 2),
                )

    def _shared_client(self) -> httpx.AsyncClient:
        if (
            self._owns_client
            and self._client is not None
            and self._client.is_closed
        ):
            self._client = None
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _create_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(connect=5, pool=1, write=10, read=30),
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
                keepalive_expiry=30,
            ),
        )

    async def _reset_owned_client(self) -> bool:
        if not self._owns_client or self._client is None:
            return False
        client = self._client
        self._client = None
        if client.is_closed:
            return True
        try:
            await client.aclose()
        except Exception as exc:
            logger.warning(
                "Runtime projection client close failed "
                "error_type=%s error=%s",
                type(exc).__name__,
                self._safe_error_message(exc),
                extra={
                    "component": "projection_sender",
                    "runtime_id": self.settings.RUNTIME_ID,
                    "error_type": type(exc).__name__,
                },
            )
        return True

    def _safe_error_message(self, exc: Exception) -> str:
        message = " ".join((str(exc) or type(exc).__name__).split())
        api_server_key = self.settings.API_SERVER_KEY.get_secret_value()
        if api_server_key:
            message = message.replace(api_server_key, "[redacted]")
        return message[:_MAX_ERROR_MESSAGE_LENGTH]

    def _endpoint(self) -> str:
        return (
            f"{self.settings.BACKEND_INTERNAL_BASE_URL.rstrip('/')}"
            "/internal/v1/runtime-projections"
        )

    def _batch_payload(
        self,
        batch: list[StoredProjection],
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for item in batch:
            payload = item.payload
            run_id = payload.get("run_id")
            raw_seq = payload.get("seq")
            seq = raw_seq if isinstance(raw_seq, int) and raw_seq > 0 else None
            items.append(
                {
                    "projection_id": item.projection_id,
                    "runtime_id": self.settings.RUNTIME_ID,
                    "projection_type": item.projection_type,
                    "occurred_at": item.created_at.isoformat(),
                    "payload_digest": canonical_payload_digest(payload),
                    "run_id": (
                        run_id
                        if isinstance(run_id, str) and run_id
                        else None
                    ),
                    "seq": seq,
                    "config_revision": payload.get("config_revision"),
                    "payload": payload,
                }
            )
        return {
            "schema_version": 1,
            "runtime_id": self.settings.RUNTIME_ID,
            "binding_id": self.settings.RUNTIME_BINDING_ID,
            "items": items,
        }
