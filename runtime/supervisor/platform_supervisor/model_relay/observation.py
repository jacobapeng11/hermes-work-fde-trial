from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
from typing import Any

from platform_supervisor.storage import SQLiteStore


logger = logging.getLogger(__name__)

_MAX_PROJECTION_ID_LENGTH = 128


def _bounded_projection_id(projection_id: str) -> str:
    if len(projection_id) <= _MAX_PROJECTION_ID_LENGTH:
        return projection_id
    digest = hashlib.sha256(projection_id.encode("utf-8")).hexdigest()
    prefix_length = _MAX_PROJECTION_ID_LENGTH - len(digest) - 1
    return f"{projection_id[:prefix_length]}:{digest}"


class RelayOutboxObservationSink:
    """Persist relay timing and usage without delaying upstream byte forwarding."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def first_upstream_byte(
        self,
        *,
        run_id: str,
        elapsed_ms: float,
    ) -> None:
        self._schedule(
            projection_id=f"{run_id}:relay:first-upstream-byte",
            projection_type="run.metric",
            payload={
                "run_id": run_id,
                "metric": "model_relay_first_upstream_byte_ms",
                "value": round(max(0.0, elapsed_ms), 3),
                "unit": "ms",
            },
        )

    def first_semantic_token(
        self,
        *,
        run_id: str,
        elapsed_ms: float,
    ) -> None:
        self._schedule(
            projection_id=f"{run_id}:relay:first-semantic-token",
            projection_type="run.metric",
            payload={
                "run_id": run_id,
                "metric": "model_relay_first_semantic_token_ms",
                "value": round(max(0.0, elapsed_ms), 3),
                "unit": "ms",
            },
        )

    def usage(
        self,
        *,
        run_id: str,
        request_id: str,
        usage: dict[str, Any],
        authorization_scope: str,
    ) -> None:
        self._schedule(
            projection_id=f"{run_id}:relay:usage:{request_id}",
            projection_type="run.usage",
            payload={
                "run_id": run_id,
                "request_id": request_id,
                "authorization_scope": authorization_scope,
                "usage": usage,
            },
        )

    def stream_summary(
        self,
        *,
        run_id: str,
        request_id: str,
        authorization_scope: str,
        status_code: int,
        content_type: str,
        content_encoding: str,
        raw_chunk_count: int,
        sse_event_count: int,
        first_chunk_bytes: int,
        max_chunk_bytes: int,
        max_chunk_gap_ms: float,
        body_duration_ms: float,
        semantic_span_ms: float | None,
        first_reasoning_delta_ms: float | None,
        first_text_delta_ms: float | None,
    ) -> None:
        del status_code, content_type, content_encoding
        metrics: tuple[tuple[str, object, str], ...] = (
            ("model_relay_upstream_raw_chunk_count", raw_chunk_count, "count"),
            ("model_relay_upstream_sse_event_count", sse_event_count, "count"),
            ("model_relay_upstream_first_chunk_bytes", first_chunk_bytes, "bytes"),
            ("model_relay_upstream_max_chunk_bytes", max_chunk_bytes, "bytes"),
            (
                "model_relay_upstream_max_chunk_gap_ms",
                round(max(0.0, max_chunk_gap_ms), 3),
                "ms",
            ),
            (
                "model_relay_upstream_body_duration_ms",
                round(max(0.0, body_duration_ms), 3),
                "ms",
            ),
            (
                "model_relay_upstream_semantic_span_ms",
                None
                if semantic_span_ms is None
                else round(max(0.0, semantic_span_ms), 3),
                "ms",
            ),
            (
                "model_relay_upstream_first_reasoning_delta_ms",
                None
                if first_reasoning_delta_ms is None
                else round(max(0.0, first_reasoning_delta_ms), 3),
                "ms",
            ),
            (
                "model_relay_upstream_first_text_delta_ms",
                None
                if first_text_delta_ms is None
                else round(max(0.0, first_text_delta_ms), 3),
                "ms",
            ),
        )
        for metric, value, unit in metrics:
            self._schedule(
                projection_id=(
                    f"{run_id}:relay:stream:{request_id}:"
                    f"{metric.removeprefix('model_relay_')}"
                ),
                projection_type="run.metric",
                payload={
                    "run_id": run_id,
                    "request_id": request_id,
                    "authorization_scope": authorization_scope,
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                },
            )

    def post_turn_activity(
        self,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        self._schedule(
            projection_id=f"{run_id}:self-review:{request_id}",
            projection_type="run.self_evolution.activity",
            payload={
                "run_id": run_id,
                "request_id": request_id,
            },
        )

    async def aclose(self) -> None:
        self._closed = True
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def _schedule(
        self,
        *,
        projection_id: str,
        projection_type: str,
        payload: dict[str, Any],
    ) -> None:
        if self._closed:
            return
        projection_id = _bounded_projection_id(projection_id)
        task = asyncio.create_task(
            self._persist(
                projection_id=projection_id,
                projection_type=projection_type,
                payload=payload,
            ),
            name=f"relay-observation:{projection_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _persist(
        self,
        *,
        projection_id: str,
        projection_type: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            await self.store.enqueue_projection(
                projection_id=projection_id,
                projection_type=projection_type,
                payload=payload,
                created_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.warning(
                "Model Relay observation persistence failed",
                extra={
                    "component": "model_relay",
                    "projection_type": projection_type,
                    "error_type": type(exc).__name__,
                },
            )
