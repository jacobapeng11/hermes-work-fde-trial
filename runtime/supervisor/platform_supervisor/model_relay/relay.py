from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import time
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import SecretStr

from platform_supervisor.model_relay.boundary import (
    MAIN_BOUNDARY_HEADER,
    MainBoundaryAction,
    MainBoundaryCoordinator,
    MainBoundaryDecision,
)
from platform_supervisor.model_relay.context_budget import (
    context_aware_max_output_tokens,
)
from platform_supervisor.settings import SupervisorSettings


logger = logging.getLogger(__name__)


class ModelRelayError(RuntimeError):
    pass


class ModelRelayRequestRejected(ModelRelayError):
    pass


class ModelRelayCapacityExhausted(ModelRelayError):
    pass


class ModelRelayBoundaryRedirected(ModelRelayError):
    """The held original request was redirected and must never reach Provider."""


class ModelRelayBoundaryUncertain(ModelRelayError):
    """A crossed/queued redirect is isolated and must never reach Provider."""


class ModelRelayBoundaryReplay(ModelRelayError):
    """An accepted marker retry replays disposition without a Provider retry."""


class _SingleChunkByteStream(httpx.AsyncByteStream):
    def __init__(self, chunk: bytes) -> None:
        self.chunk = chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.chunk

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ActiveModelRoute:
    route_token: str
    run_id: str
    session_id: str
    model_id: str
    api_mode: str
    max_output_tokens: int
    expires_at: datetime
    upstream_model_id: str | None = None
    context_length: int = 128_000
    # ``model_id`` remains the platform-controlled ModelEntry id.  Hermes
    # sends ``runtime_model_id`` so its provider compatibility policy sees the
    # upstream model name instead of an opaque catalog id.
    runtime_model_id: str | None = None
    runtime_capabilities: dict[str, str] | None = None
    authorization_scope: Literal[
        "active_run",
        "post_turn_review",
        "kanban_worker",
    ] = "active_run"
    activation_deadline_at: datetime | None = None
    last_activity_at: datetime | None = None
    idle_timeout_seconds: int = 15
    provider_base_url: str | None = None
    provider_api_key: SecretStr | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.provider_api_key, str):
            object.__setattr__(
                self,
                "provider_api_key",
                SecretStr(self.provider_api_key),
            )
        if (self.provider_base_url is None) != (self.provider_api_key is None):
            raise ValueError("provider route URL and key must be supplied together")
        if self.provider_base_url is not None and not self.provider_base_url.startswith(
            "https://"
        ):
            raise ValueError("provider route URL must use HTTPS")
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.route_token) is None:
            raise ValueError("route_token must be a URL-safe opaque value")
        for name, value in (
            ("run_id", self.run_id),
            ("session_id", self.session_id),
            ("model_id", self.model_id),
        ):
            if not value:
                raise ValueError(f"{name} cannot be empty")
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if (
            self.activation_deadline_at is not None
            and self.activation_deadline_at.tzinfo is None
        ):
            raise ValueError(
                "activation_deadline_at must be timezone-aware"
            )
        if (
            self.last_activity_at is not None
            and self.last_activity_at.tzinfo is None
        ):
            raise ValueError("last_activity_at must be timezone-aware")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.context_length <= 0:
            raise ValueError("context_length must be positive")
        if self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")

    @property
    def gateway_model_id(self) -> str:
        return self.upstream_model_id or self.model_id

    @property
    def hermes_model_id(self) -> str:
        return self.runtime_model_id or self.gateway_model_id

    @property
    def reasoning_replay(self) -> str:
        return str((self.runtime_capabilities or {}).get(
            "reasoning_replay", "not_supported"
        ))


class ModelRouteRegistry:
    def __init__(self) -> None:
        self._routes: dict[str, ActiveModelRoute] = {}
        self._model_listings: dict[str, str] = {}
        self._pending_post_turn: set[str] = set()
        self._route_ready: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        route: ActiveModelRoute,
        *,
        pending: bool = False,
    ) -> None:
        if pending and route.authorization_scope != "post_turn_review":
            raise ValueError("only post-turn routes may be pending")
        async with self._lock:
            existing = self._routes.get(route.route_token)
            if existing is not None and existing != route:
                raise ModelRelayRequestRejected(
                    "model relay route token is already bound to another Run"
                )
            self._routes[route.route_token] = route
            self._model_listings.setdefault(
                route.route_token,
                route.hermes_model_id,
            )
            if pending:
                self._pending_post_turn.add(route.route_token)
                self._route_ready.setdefault(
                    route.route_token,
                    asyncio.Event(),
                )

    async def register_kanban_worker(
        self,
        route: ActiveModelRoute,
    ) -> None:
        """Register or refresh one profile-scoped Kanban worker lease."""
        if route.authorization_scope != "kanban_worker":
            raise ValueError("kanban worker route scope is required")
        now = datetime.now(timezone.utc)
        async with self._lock:
            existing = self._routes.get(route.route_token)
            if (
                existing is not None
                and existing.run_id != route.run_id
                and existing.expires_at > now
            ):
                raise ModelRelayRequestRejected(
                    "kanban worker route is already bound to another Run"
                )
            self._routes[route.route_token] = route
            self._model_listings[route.route_token] = route.hermes_model_id

    async def revoke_if_run(
        self,
        route_token: str,
        run_id: str,
    ) -> bool:
        """Revoke a route only when it still belongs to the supplied Run."""
        async with self._lock:
            route = self._routes.get(route_token)
            if route is None or route.run_id != run_id:
                return False
            self._routes.pop(route_token, None)
            self._model_listings.pop(route_token, None)
            self._pending_post_turn.discard(route_token)
            ready = self._route_ready.pop(route_token, None)
            if ready is not None:
                ready.set()
            return True

    async def renew_active_run_route(
        self,
        route_token: str,
        run_id: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        """Extend only the exact active-run route owned by ``run_id``."""
        now = datetime.now(timezone.utc)
        async with self._lock:
            route = self._routes.get(route_token)
            if (
                route is None
                or route.run_id != run_id
                or route.authorization_scope != "active_run"
            ):
                return False
            self._routes[route_token] = replace(
                route,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            return True

    async def register_model_listing(
        self, route_token: str, model_id: str
    ) -> None:
        if not model_id:
            raise ValueError("model_id cannot be empty")
        async with self._lock:
            self._model_listings[route_token] = model_id

    async def model_listing(self, route_token: str) -> str:
        async with self._lock:
            model_id = self._model_listings.get(route_token)
        if not model_id:
            raise ModelRelayRequestRejected("model relay route is unavailable")
        return model_id

    async def get(
        self,
        route_token: str,
        *,
        touch_post_turn: bool = True,
    ) -> ActiveModelRoute:
        async with self._lock:
            route = self._routes.get(route_token)
            ready = (
                self._route_ready.get(route_token)
                if route_token in self._pending_post_turn
                else None
            )
        if route is None:
            raise ModelRelayRequestRejected("model relay route is unavailable")
        if ready is not None:
            try:
                await asyncio.wait_for(ready.wait(), timeout=10)
            except TimeoutError as exc:
                raise ModelRelayRequestRejected(
                    "post-turn model route is not active"
                ) from exc
            async with self._lock:
                route = self._routes.get(route_token)
        if route is None:
            raise ModelRelayRequestRejected("model relay route is unavailable")
        now = datetime.now(timezone.utc)
        expired = route.expires_at <= now
        if (
            route.authorization_scope == "post_turn_review"
            and touch_post_turn
        ):
            expired = expired or (
                route.last_activity_at is None
                and (
                    route.activation_deadline_at is None
                    or route.activation_deadline_at <= now
                )
            )
            expired = expired or (
                route.last_activity_at is not None
                and (
                    now - route.last_activity_at
                ).total_seconds() > route.idle_timeout_seconds
            )
        if expired:
            await self.revoke(route_token)
            raise ModelRelayRequestRejected("model relay route has expired")
        if route.authorization_scope == "post_turn_review":
            route = replace(route, last_activity_at=now)
            async with self._lock:
                if self._routes.get(route_token) is not None:
                    self._routes[route_token] = route
        return route

    async def revoke(self, route_token: str) -> None:
        async with self._lock:
            self._routes.pop(route_token, None)
            self._model_listings.pop(route_token, None)
            self._pending_post_turn.discard(route_token)
            ready = self._route_ready.pop(route_token, None)
            if ready is not None:
                ready.set()

    async def revoke_run(self, run_id: str) -> None:
        async with self._lock:
            expired = [
                token
                for token, route in self._routes.items()
                if route.run_id == run_id
            ]
            for token in expired:
                self._routes.pop(token, None)
                self._pending_post_turn.discard(token)
                ready = self._route_ready.pop(token, None)
                if ready is not None:
                    ready.set()

    async def complete_run(
        self,
        run_id: str,
        *,
        completed: bool,
        activated_at: datetime,
        activation_window_seconds: int,
        max_duration_seconds: int,
    ) -> None:
        async with self._lock:
            matching = [
                (token, route)
                for token, route in self._routes.items()
                if route.run_id == run_id
            ]
            for token, route in matching:
                if route.authorization_scope == "active_run":
                    self._routes.pop(token, None)
                    continue
                if not completed:
                    self._routes.pop(token, None)
                    self._pending_post_turn.discard(token)
                    ready = self._route_ready.pop(token, None)
                    if ready is not None:
                        ready.set()
                    continue
                self._routes[token] = replace(
                    route,
                    expires_at=activated_at
                    + timedelta(seconds=max_duration_seconds),
                    activation_deadline_at=activated_at
                    + timedelta(seconds=activation_window_seconds),
                )
                self._pending_post_turn.discard(token)
                ready = self._route_ready.pop(token, None)
                if ready is not None:
                    ready.set()


class RelayObservationSink(Protocol):
    def first_upstream_byte(
        self,
        *,
        run_id: str,
        elapsed_ms: float,
    ) -> None: ...

    def first_semantic_token(
        self,
        *,
        run_id: str,
        elapsed_ms: float,
    ) -> None: ...

    def usage(
        self,
        *,
        run_id: str,
        request_id: str,
        usage: dict[str, Any],
        authorization_scope: str,
    ) -> None: ...

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
    ) -> None: ...

    def post_turn_activity(
        self,
        *,
        run_id: str,
        request_id: str,
    ) -> None: ...


class _NoopObservationSink:
    def first_upstream_byte(self, *, run_id: str, elapsed_ms: float) -> None:
        return None

    def first_semantic_token(self, *, run_id: str, elapsed_ms: float) -> None:
        return None

    def usage(
        self,
        *,
        run_id: str,
        request_id: str,
        usage: dict[str, Any],
        authorization_scope: str,
    ) -> None:
        return None

    def stream_summary(self, **summary: object) -> None:
        return None

    def post_turn_activity(
        self,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        return None


@dataclass(slots=True)
class _SseChunkObservation:
    first_semantic: bool
    semantic: bool
    text_delta: bool
    reasoning_delta: bool
    usages: list[dict[str, Any]]


class _SseObservationParser:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self.first_semantic_seen = False
        self.sse_event_count = 0

    def feed(self, chunk: bytes) -> _SseChunkObservation:
        self._buffer.extend(chunk)
        first_semantic = False
        semantic = False
        text_delta = False
        reasoning_delta = False
        usages: list[dict[str, Any]] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > 64 * 1024:
                    self._buffer.clear()
                break
            raw_line = bytes(self._buffer[:newline]).rstrip(b"\r")
            del self._buffer[: newline + 1]
            if not raw_line.startswith(b"data:"):
                continue
            raw_data = raw_line[5:].strip()
            if not raw_data:
                continue
            self.sse_event_count += 1
            if raw_data == b"[DONE]":
                continue
            try:
                payload = json.loads(raw_data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            usage = payload.get("usage")
            if isinstance(usage, dict):
                usages.append(usage)
            payload_text, payload_reasoning = self._semantic_kinds(payload)
            payload_semantic = payload_text or payload_reasoning
            text_delta = text_delta or payload_text
            reasoning_delta = reasoning_delta or payload_reasoning
            semantic = semantic or payload_semantic
            if not self.first_semantic_seen and payload_semantic:
                self.first_semantic_seen = True
                first_semantic = True
        return _SseChunkObservation(
            first_semantic=first_semantic,
            semantic=semantic,
            text_delta=text_delta,
            reasoning_delta=reasoning_delta,
            usages=usages,
        )

    @staticmethod
    def _semantic_kinds(payload: dict[str, Any]) -> tuple[bool, bool]:
        event_type = payload.get("type")
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            return isinstance(delta, str) and bool(delta), False
        if event_type == "response.reasoning_summary_text.delta":
            delta = payload.get("delta")
            return False, isinstance(delta, str) and bool(delta)
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return False, False
        text_delta = False
        reasoning_delta = False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            value = delta.get("content")
            text_delta = text_delta or (isinstance(value, str) and bool(value))
            for key in ("reasoning_content", "reasoning"):
                value = delta.get(key)
                reasoning_delta = reasoning_delta or (
                    isinstance(value, str) and bool(value)
                )
        return text_delta, reasoning_delta


@dataclass(slots=True)
class RelayResponse:
    status_code: int
    headers: dict[str, str]
    body_iterator: AsyncIterator[bytes]


class ModelRelay:
    def __init__(
        self,
        settings: SupervisorSettings,
        registry: ModelRouteRegistry,
        *,
        client: httpx.AsyncClient | None = None,
        observation_sink: RelayObservationSink | None = None,
        main_boundary: MainBoundaryCoordinator | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self._client = client
        self._client_lock = asyncio.Lock()
        self._observation_sink = observation_sink or _NoopObservationSink()
        self._main_boundary = main_boundary
        self._clock = clock
        self._warmup_task: asyncio.Task[None] | None = None
        self._provider_warmup_attempted: set[str] = set()
        self._provider_warmup_tasks: set[asyncio.Task[None]] = set()
        self._active_requests = 0
        self._capacity_rejections = 0
        self._reasoning_replay_stats = {
            "reasoning_replay.required_requests": 0,
            "reasoning_replay.real_replayed": 0,
            "reasoning_replay.promoted_from_internal": 0,
            "reasoning_replay.fallback_space": 0,
            "reasoning_replay.stripped": 0,
            "thinking_mode.enabled_requests": 0,
        }
        self._warmup_status = "not_started"

    def metrics_snapshot(self) -> dict[str, int | str]:
        return {
            "active": self._active_requests,
            "capacity": self.settings.model_relay_max_connections,
            "capacity_rejections": self._capacity_rejections,
            "transport_warmup": self._warmup_status,
            **self._reasoning_replay_stats,
        }

    async def shared_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    trust_env=False,
                    http2=False,
                    limits=httpx.Limits(
                        max_connections=self.settings.model_relay_max_connections,
                        max_keepalive_connections=(
                            self.settings.model_relay_max_keepalive_connections
                        ),
                        keepalive_expiry=(
                            self.settings.APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS
                        ),
                    ),
                    timeout=httpx.Timeout(
                        connect=5.0,
                        pool=1.0,
                        write=30.0,
                        read=600.0,
                    ),
                )
            return self._client

    def start_transport_warmup(self) -> None:
        if self._warmup_task is None:
            self._warmup_task = asyncio.create_task(
                self._transport_warmup(),
                name="model-relay-transport-warmup",
            )

    async def wait_for_transport_warmup(self) -> None:
        if self._warmup_task is not None:
            await self._warmup_task

    async def forward(
        self,
        *,
        route_token: str,
        operation: str,
        body: bytes,
        content_type: str = "application/json",
        request_headers: Mapping[str, str] | None = None,
    ) -> RelayResponse:
        route = await self.registry.get(
            route_token,
            touch_post_turn=False,
        )
        relay_request_id = uuid4().hex
        normalized_operation = operation.strip("/")
        self._validate_operation(route, normalized_operation)
        forwarded_body = self._validate_request_body(route, body)
        boundary_decision = MainBoundaryDecision(
            action=MainBoundaryAction.FORWARD,
            disposition="disabled",
        )
        if self._main_boundary is not None:
            marker_header = self._header_value(
                request_headers,
                MAIN_BOUNDARY_HEADER,
            )
            boundary_decision = await self._main_boundary.before_forward(
                route=route,
                marker_header=marker_header,
                request_body=forwarded_body,
            )
            if boundary_decision.action is MainBoundaryAction.ABORT:
                raise ModelRelayBoundaryRedirected(
                    f"main boundary request {boundary_decision.disposition}"
                )
            if boundary_decision.action is MainBoundaryAction.ISOLATE:
                raise ModelRelayBoundaryUncertain(
                    f"main boundary request {boundary_decision.disposition}"
                )
            if boundary_decision.action is MainBoundaryAction.REPLAY:
                raise ModelRelayBoundaryReplay(
                    "main boundary request already "
                    f"{boundary_decision.disposition}"
                )
        if route.authorization_scope == "post_turn_review":
            route = await self.registry.get(route_token)
            self._observe_safely(
                lambda: self._observation_sink.post_turn_activity(
                    run_id=route.run_id,
                    request_id=relay_request_id,
                )
            )
        provider_base_url = route.provider_base_url or self.settings.NEWAPI_BASE_URL
        provider_api_key = (
            route.provider_api_key.get_secret_value()
            if route.provider_api_key is not None
            else self.settings.NEWAPI_API_KEY.get_secret_value()
        )
        # The process-level warmup already covers the configured gateway.  A
        # route-specific warmup is needed only when the signed route points at
        # another Provider target.
        if route.provider_base_url is not None:
            self.schedule_provider_warmup(provider_base_url, provider_api_key)
        url = self._join_url(provider_base_url, normalized_operation)
        client = await self.shared_client()
        upstream_request = client.build_request(
            "POST",
            url,
            content=forwarded_body,
            headers={
                "Authorization": (
                    f"Bearer {provider_api_key}"
                ),
                "Content-Type": content_type,
                "Accept": "text/event-stream, application/json",
                # The relay forwards response bytes unchanged via aiter_raw().
                # Require identity encoding so Hermes never receives a
                # compressed body without a matching decoding contract.
                "Accept-Encoding": "identity",
            },
        )
        request_started_at = self._clock()
        self._active_requests += 1
        response: httpx.Response | None = None
        boundary_task: asyncio.Task[MainBoundaryDecision] | None = None
        send_task: asyncio.Task[httpx.Response] | None = None
        race_boundary = (
            self._main_boundary is not None
            and boundary_decision.disposition == "awaiting_upstream_active"
        )

        async def cancel_racing_tasks() -> None:
            for task in (boundary_task, send_task):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if response is not None:
                await response.aclose()

        try:
            if race_boundary:
                # Starting the upstream request establishes the active-model
                # boundary.  Dispatch the durable correction concurrently so
                # a redirect does not wait for a provider response that will
                # be discarded anyway.
                send_task = asyncio.create_task(
                    client.send(upstream_request, stream=True),
                    name="model-relay-upstream-request",
                )
                await asyncio.sleep(0)
                assert self._main_boundary is not None
                boundary_task = asyncio.create_task(
                    self._main_boundary.mark_upstream_active(
                        boundary_decision
                    ),
                    name="model-relay-boundary-dispatch",
                )
                done, _ = await asyncio.wait(
                    {send_task, boundary_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if boundary_task in done:
                    boundary_decision = await boundary_task
                    if send_task in done:
                        response = send_task.result()
                    if boundary_decision.action is not MainBoundaryAction.FORWARD:
                        if not send_task.done():
                            send_task.cancel()
                            await asyncio.gather(
                                send_task,
                                return_exceptions=True,
                            )
                    else:
                        response = await send_task
                if boundary_task is not None and not boundary_task.done():
                    response = await send_task
                    boundary_decision = await boundary_task
            else:
                response = await client.send(upstream_request, stream=True)
        except httpx.PoolTimeout as exc:
            await cancel_racing_tasks()
            if self._main_boundary is not None:
                await self._main_boundary.mark_completed(boundary_decision)
            self._active_requests = max(0, self._active_requests - 1)
            self._capacity_rejections += 1
            raise ModelRelayCapacityExhausted(
                "MODEL_RELAY_CAPACITY_EXHAUSTED"
            ) from exc
        except httpx.HTTPError as exc:
            await cancel_racing_tasks()
            if self._main_boundary is not None:
                await self._main_boundary.mark_completed(boundary_decision)
            self._active_requests = max(0, self._active_requests - 1)
            raise ModelRelayError("model relay upstream request failed") from exc
        except BaseException:
            await cancel_racing_tasks()
            if self._main_boundary is not None:
                await self._main_boundary.mark_completed(boundary_decision)
            self._active_requests = max(0, self._active_requests - 1)
            raise
        if self._main_boundary is not None:
            if not race_boundary:
                boundary_decision = await self._main_boundary.mark_upstream_active(
                    boundary_decision
                )
            if boundary_decision.action is not MainBoundaryAction.FORWARD:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    self._active_requests = max(0, self._active_requests - 1)
                if boundary_decision.action is MainBoundaryAction.ABORT:
                    raise ModelRelayBoundaryRedirected(
                        f"main boundary request {boundary_decision.disposition}"
                    )
                if boundary_decision.action is MainBoundaryAction.ISOLATE:
                    raise ModelRelayBoundaryUncertain(
                        f"main boundary request {boundary_decision.disposition}"
                    )
                raise ModelRelayBoundaryReplay(
                    "main boundary request already "
                    f"{boundary_decision.disposition}"
                )
        if response is not None and response.status_code == 400:
            (
                original_error_body,
                retry_body,
                error_headers,
                error_request,
            ) = await self._read_output_cap_retry_body(
                route,
                forwarded_body,
                response,
            )
            if retry_body is None:
                response = httpx.Response(
                    400,
                    headers=error_headers,
                    stream=_SingleChunkByteStream(original_error_body),
                    request=error_request,
                )
            else:
                retry_headers = {
                    key: value
                    for key, value in upstream_request.headers.items()
                    if key.lower() != "content-length"
                }
                try:
                    response = await client.send(
                        client.build_request(
                            "POST",
                            url,
                            content=retry_body,
                            headers=retry_headers,
                        ),
                        stream=True,
                    )
                except httpx.PoolTimeout as exc:
                    if self._main_boundary is not None:
                        await self._main_boundary.mark_completed(
                            boundary_decision
                        )
                    self._active_requests = max(0, self._active_requests - 1)
                    self._capacity_rejections += 1
                    raise ModelRelayCapacityExhausted(
                        "MODEL_RELAY_CAPACITY_EXHAUSTED"
                    ) from exc
                except httpx.HTTPError as exc:
                    if self._main_boundary is not None:
                        await self._main_boundary.mark_completed(
                            boundary_decision
                        )
                    self._active_requests = max(0, self._active_requests - 1)
                    raise ModelRelayError(
                        "model relay upstream request failed"
                    ) from exc
                logger.warning(
                    "Model Relay retried output-cap request with a lower token limit",
                    extra={
                        "component": "model_relay",
                        "event": "output_cap_retry",
                        "run_id": route.run_id,
                        "authorization_scope": route.authorization_scope,
                    },
                )
        parser = _SseObservationParser()
        first_byte_seen = False
        latest_usage: dict[str, Any] | None = None
        raw_chunk_count = 0
        first_chunk_bytes = 0
        max_chunk_bytes = 0
        max_chunk_gap_ms = 0.0
        previous_chunk_at: float | None = None
        first_semantic_at: float | None = None
        last_semantic_at: float | None = None
        first_reasoning_delta_ms: float | None = None
        first_text_delta_ms: float | None = None

        async def body_iterator() -> AsyncIterator[bytes]:
            nonlocal first_byte_seen, latest_usage, raw_chunk_count
            nonlocal first_chunk_bytes, max_chunk_bytes, max_chunk_gap_ms
            nonlocal previous_chunk_at, first_semantic_at, last_semantic_at
            nonlocal first_reasoning_delta_ms, first_text_delta_ms
            try:
                async for chunk in response.aiter_raw():
                    if not chunk:
                        continue
                    observed_at = self._clock()
                    yield chunk
                    raw_chunk_count += 1
                    chunk_bytes = len(chunk)
                    if raw_chunk_count == 1:
                        first_chunk_bytes = chunk_bytes
                    max_chunk_bytes = max(max_chunk_bytes, chunk_bytes)
                    if previous_chunk_at is not None:
                        max_chunk_gap_ms = max(
                            max_chunk_gap_ms,
                            (observed_at - previous_chunk_at) * 1000,
                        )
                    previous_chunk_at = observed_at
                    elapsed_ms = max(0.0, (observed_at - request_started_at) * 1000)
                    if (
                        not first_byte_seen
                        and route.authorization_scope == "active_run"
                    ):
                        first_byte_seen = True
                        self._observe_safely(
                            lambda: self._observation_sink.first_upstream_byte(
                                run_id=route.run_id,
                                elapsed_ms=elapsed_ms,
                            )
                        )
                    try:
                        chunk_observation = parser.feed(chunk)
                    except Exception:
                        continue
                    if (
                        chunk_observation.first_semantic
                        and route.authorization_scope == "active_run"
                    ):
                        self._observe_safely(
                            lambda: self._observation_sink.first_semantic_token(
                                run_id=route.run_id,
                                elapsed_ms=elapsed_ms,
                            )
                        )
                    if chunk_observation.semantic:
                        if first_semantic_at is None:
                            first_semantic_at = observed_at
                        last_semantic_at = observed_at
                    if (
                        chunk_observation.reasoning_delta
                        and first_reasoning_delta_ms is None
                    ):
                        first_reasoning_delta_ms = elapsed_ms
                    if (
                        chunk_observation.text_delta
                        and first_text_delta_ms is None
                    ):
                        first_text_delta_ms = elapsed_ms
                    if chunk_observation.usages:
                        latest_usage = chunk_observation.usages[-1]
            finally:
                completed_at = self._clock()
                body_duration_ms = max(
                    0.0,
                    (completed_at - request_started_at) * 1000,
                )
                semantic_span_ms = (
                    max(0.0, (last_semantic_at - first_semantic_at) * 1000)
                    if first_semantic_at is not None
                    and last_semantic_at is not None
                    else None
                )
                media_type = response.headers.get("content-type", "")
                media_type = media_type.split(";", 1)[0].strip().lower()[:64]
                content_encoding_present = bool(
                    response.headers.get("content-encoding", "").strip()
                )
                stream_summary = {
                    "run_id": route.run_id,
                    "request_id": relay_request_id,
                    "authorization_scope": route.authorization_scope,
                    "status_code": response.status_code,
                    "content_type": media_type,
                    "content_encoding": (
                        "present" if content_encoding_present else "identity"
                    ),
                    "raw_chunk_count": raw_chunk_count,
                    "sse_event_count": parser.sse_event_count,
                    "first_chunk_bytes": first_chunk_bytes,
                    "max_chunk_bytes": max_chunk_bytes,
                    "max_chunk_gap_ms": max(0.0, max_chunk_gap_ms),
                    "body_duration_ms": body_duration_ms,
                    "semantic_span_ms": semantic_span_ms,
                    "first_reasoning_delta_ms": first_reasoning_delta_ms,
                    "first_text_delta_ms": first_text_delta_ms,
                }
                self._observe_safely(
                    lambda: self._observation_sink.stream_summary(
                        **stream_summary,
                    )
                )
                logger.info(
                    "Model Relay stream completed",
                    extra={
                        "component": "model_relay",
                        "run_id": route.run_id,
                        "upstream_status_code": response.status_code,
                        "upstream_media_type": media_type,
                        "upstream_content_encoding_present": (
                            content_encoding_present
                        ),
                        "upstream_raw_chunk_count": raw_chunk_count,
                        "upstream_sse_event_count": parser.sse_event_count,
                        "upstream_first_chunk_bytes": first_chunk_bytes,
                        "upstream_max_chunk_bytes": max_chunk_bytes,
                        "upstream_max_chunk_gap_ms": max_chunk_gap_ms,
                        "upstream_body_duration_ms": body_duration_ms,
                        "upstream_semantic_span_ms": semantic_span_ms,
                        "upstream_first_reasoning_delta_ms": (
                            first_reasoning_delta_ms
                        ),
                        "upstream_first_text_delta_ms": first_text_delta_ms,
                    },
                )
                if latest_usage is not None:
                    self._observe_safely(
                        lambda: self._observation_sink.usage(
                            run_id=route.run_id,
                            request_id=relay_request_id,
                            usage=latest_usage,
                            authorization_scope=(
                                route.authorization_scope
                            ),
                        )
                    )
                await response.aclose()
                if self._main_boundary is not None:
                    await self._main_boundary.mark_completed(
                        boundary_decision
                    )
                self._active_requests = max(0, self._active_requests - 1)

        return RelayResponse(
            status_code=response.status_code,
            headers=self._response_headers(response.headers),
            body_iterator=body_iterator(),
        )

    async def aclose(self) -> None:
        if self._warmup_task is not None:
            await asyncio.gather(self._warmup_task, return_exceptions=True)
            self._warmup_task = None
        provider_warmup_tasks = tuple(self._provider_warmup_tasks)
        for task in provider_warmup_tasks:
            task.cancel()
        if provider_warmup_tasks:
            await asyncio.gather(*provider_warmup_tasks, return_exceptions=True)
        self._provider_warmup_tasks.clear()
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        close_sink = getattr(self._observation_sink, "aclose", None)
        if callable(close_sink):
            await close_sink()

    async def _transport_warmup(self) -> None:
        self._warmup_status = "warming"
        try:
            warmed = await self._warm_provider_route(
                self.settings.NEWAPI_BASE_URL,
                self.settings.NEWAPI_API_KEY.get_secret_value(),
            )
            self._warmup_status = "ready" if warmed else "failed"
        except Exception as exc:
            self._warmup_status = "failed"
            logger.warning(
                "Model Relay transport warmup failed",
                extra={
                    "component": "model_relay",
                    "event": "transport_warmup_failed",
                    "error_type": type(exc).__name__,
                },
            )

    def schedule_provider_warmup(
        self,
        provider_base_url: str,
        provider_api_key: str,
    ) -> None:
        """Warm the exact route without delaying the active model request.

        Startup warmup only knows the global gateway settings. Published model
        routes can point at a different Provider (for example DashScope), so
        the first request on that route must also establish its own keep-alive
        connection. This is best-effort and never participates in request
        authorization or failure handling.
        """
        target_key = hashlib.sha256(
            f"{provider_base_url.rstrip('/')}\0{provider_api_key}".encode()
        ).hexdigest()
        if target_key in self._provider_warmup_attempted:
            return
        self._provider_warmup_attempted.add(target_key)
        task = asyncio.create_task(
            self._warm_provider_route(provider_base_url, provider_api_key),
            name="model-relay-provider-warmup",
        )
        self._provider_warmup_tasks.add(task)
        task.add_done_callback(self._provider_warmup_tasks.discard)

    async def _warm_provider_route(
        self,
        provider_base_url: str,
        provider_api_key: str,
    ) -> bool:
        try:
            client = await self.shared_client()
            response = await client.get(
                self._join_url(provider_base_url, "models"),
                headers={"Authorization": f"Bearer {provider_api_key}"},
                timeout=5.0,
            )
            await response.aclose()
        except Exception as exc:
            logger.warning(
                "Model Relay provider route warmup failed",
                extra={
                    "component": "model_relay",
                    "event": "provider_route_warmup_failed",
                    "error_type": type(exc).__name__,
                    "provider_base_url": provider_base_url,
                },
            )
            return False
        return True

    @staticmethod
    def _validate_operation(route: ActiveModelRoute, operation: str) -> None:
        expected = {
            "chat_completions": "chat/completions",
            "responses": "responses",
            "anthropic": "messages",
        }.get(route.api_mode)
        if expected is None or operation != expected:
            raise ModelRelayRequestRejected(
                "model relay operation is not allowed for this Run"
            )

    def _validate_request_body(self, route: ActiveModelRoute, body: bytes) -> bytes:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelRelayRequestRejected("model request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise ModelRelayRequestRejected("model request body must be an object")
        if payload.get("model") != route.hermes_model_id:
            raise ModelRelayRequestRejected("requested model is not allowed for this Run")
        self._apply_reasoning_replay_policy(route, payload)
        request_mutated = self._apply_thinking_mode_policy(
            route,
            payload,
        )
        limit_keys = (
            ("max_output_tokens",)
            if route.api_mode == "responses"
            else ("max_completion_tokens", "max_tokens")
        )
        configured_limit: object | None = None
        configured_limit_key: str | None = None
        for key in limit_keys:
            if key in payload:
                configured_limit = payload[key]
                configured_limit_key = key
                break
        if configured_limit_key is None:
            configured_limit_key = limit_keys[-1]
            configured_limit = context_aware_max_output_tokens(
                route.max_output_tokens,
                route.context_length,
            )
            payload[configured_limit_key] = configured_limit
            request_mutated = True
        if (
            isinstance(configured_limit, bool)
            or not isinstance(configured_limit, int)
            or configured_limit <= 0
        ):
            raise ModelRelayRequestRejected(
                "model request must include a positive output token limit"
            )
        authorized_limit = context_aware_max_output_tokens(
            route.max_output_tokens,
            route.context_length,
        )
        if configured_limit > authorized_limit:
            # Hermes may carry its process-level default (currently 65536) in
            # a per-session request even when the signed Run grant has a lower
            # maximum.  The Relay is the authorization boundary: normalize the
            # upstream request to the grant rather than rejecting an otherwise
            # authorized turn.  This never broadens the grant and preserves a
            # smaller client-requested limit unchanged.
            payload[configured_limit_key] = authorized_limit
            request_mutated = True
        if (
            route.gateway_model_id == route.hermes_model_id
            and not request_mutated
            and route.reasoning_replay == "optional"
        ):
            return body
        payload["model"] = route.gateway_model_id
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _apply_thinking_mode_policy(
        self,
        route: ActiveModelRoute,
        payload: dict[str, Any],
    ) -> bool:
        thinking_mode = str(
            (route.runtime_capabilities or {}).get(
                "thinking_mode",
                "provider_default",
            )
        )
        if thinking_mode == "enabled":
            self._reasoning_replay_stats[
                "thinking_mode.enabled_requests"
            ] += 1
            desired = {"type": "enabled"}
            if payload.get("thinking") == desired:
                return False
            payload["thinking"] = desired
            return True
        if thinking_mode == "disabled" and "thinking" in payload:
            payload.pop("thinking", None)
            return True
        return False

    def _apply_reasoning_replay_policy(
        self,
        route: ActiveModelRoute,
        payload: dict[str, Any],
    ) -> None:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        replay_mode = route.reasoning_replay
        if replay_mode == "required":
            self._reasoning_replay_stats["reasoning_replay.required_requests"] += 1
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            if replay_mode == "not_supported":
                if message.pop("reasoning_content", None) is not None:
                    self._reasoning_replay_stats["reasoning_replay.stripped"] += 1
                continue
            if replay_mode != "required":
                continue
            existing = message.get("reasoning_content")
            if isinstance(existing, str) and existing.strip():
                self._reasoning_replay_stats["reasoning_replay.real_replayed"] += 1
                continue
            internal_reasoning = message.get("reasoning")
            if isinstance(internal_reasoning, str) and internal_reasoning.strip():
                message["reasoning_content"] = internal_reasoning
                self._reasoning_replay_stats[
                    "reasoning_replay.promoted_from_internal"
                ] += 1
                continue
            # This can only make unrecoverable old history protocol-valid.  It
            # must never be mistaken for recovered chain-of-thought.
            message["reasoning_content"] = " "
            self._reasoning_replay_stats["reasoning_replay.fallback_space"] += 1
            logger.warning(
                "Reasoning replay used legacy fallback space",
                extra={
                    "component": "model_relay",
                    "event": "reasoning_replay_fallback_space",
                    "run_id": route.run_id,
                    "session_id": route.session_id,
                    "platform_model_id": route.model_id,
                    "upstream_model_id": route.gateway_model_id,
                },
            )

    @staticmethod
    def _output_cap_retry_body(
        route: ActiveModelRoute,
        request_body: bytes,
        error_body: bytes,
    ) -> bytes | None:
        """Return a retried body for an unambiguous provider output-cap 400."""
        try:
            error_document = json.loads(error_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_document = None
        error_message: object = None
        if isinstance(error_document, dict):
            error = error_document.get("error")
            if isinstance(error, dict):
                error_message = error.get("message")
            if error_message is None:
                error_message = error_document.get("message")
        text = str(error_message or "").lower()
        if not text:
            try:
                text = error_body.decode("utf-8", errors="replace").lower()
            except UnicodeError:
                return None

        context_match = re.search(
            r"maximum context length is (\d+)", text
        )
        output_match = re.search(r"requested (\d+) output tokens", text)
        input_match = re.search(
            r"prompt contains (?:at least )?(\d+) input tokens", text
        )
        if not context_match or not output_match or not input_match:
            return None

        context_length = int(context_match.group(1))
        input_tokens = int(input_match.group(1))
        safe_output = context_length - input_tokens - 64
        safe_output = min(
            safe_output,
            context_aware_max_output_tokens(
                route.max_output_tokens,
                route.context_length,
            ),
        )
        if safe_output < 1024:
            return None

        try:
            payload = json.loads(request_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        limit_keys = (
            ("max_output_tokens",)
            if route.api_mode == "responses"
            else ("max_completion_tokens", "max_tokens")
        )
        limit_key = next((key for key in limit_keys if key in payload), limit_keys[-1])
        current_limit = payload.get(limit_key)
        if (
            isinstance(current_limit, int)
            and not isinstance(current_limit, bool)
            and current_limit <= safe_output
        ):
            return None
        payload[limit_key] = safe_output
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

    async def _read_output_cap_retry_body(
        self,
        route: ActiveModelRoute,
        request_body: bytes,
        response: httpx.Response,
    ) -> tuple[bytes, bytes | None, httpx.Headers, httpx.Request]:
        original_body = await response.aread()
        headers = response.headers
        request = response.request
        await response.aclose()
        adjusted_body = self._output_cap_retry_body(
            route,
            request_body,
            original_body,
        )
        return original_body, adjusted_body, headers, request

    @staticmethod
    def _response_headers(headers: httpx.Headers) -> dict[str, str]:
        allowed = {
            "content-type",
            "x-request-id",
            "openai-processing-ms",
            "retry-after",
        }
        return {
            key: value
            for key, value in headers.items()
            if key.lower() in allowed
        }

    @staticmethod
    def _header_value(
        headers: Mapping[str, str] | None,
        name: str,
    ) -> str | None:
        if headers is None:
            return None
        lowered = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lowered:
                return str(value)
        return None

    @staticmethod
    def _join_url(base_url: str, path: str) -> str:
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _observe_safely(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            return
