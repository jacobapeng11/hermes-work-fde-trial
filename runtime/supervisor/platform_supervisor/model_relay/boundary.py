from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import logging
import re
from typing import Any, Protocol


MAIN_BOUNDARY_HEADER = "X-Hermes-Platform-Main-Boundary"
MAIN_BOUNDARY_HEADER_MAX_BYTES = 4096
MAIN_BOUNDARY_CLOCK_SKEW_SECONDS = 5.0
MAIN_BOUNDARY_MAX_TTL_SECONDS = 30.0
_MAX_JCS_SAFE_INTEGER = 9_007_199_254_740_991
_ROUTE_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+")
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RFC3339_MILLISECONDS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
)
_MARKER_FIELDS = {
    "contract_version",
    "route_token",
    "session_id",
    "task_id",
    "turn_id",
    "api_request_id",
    "api_call_count",
    "boundary_epoch_nonce",
    "request_nonce",
    "issued_at",
    "expires_at",
}
_BOUNDARY_HANDLE_AUTHORITY = object()
_DECISION_AUTHORITY = object()

logger = logging.getLogger(__name__)


def _log_handle_receipt(stage: str, run_id: str, **facts: object) -> None:
    receipt = {
        "event": "followup_boundary_handle_receipt",
        "followup_stage": stage,
        "parent_run_id": run_id,
        **facts,
    }
    logger.warning(
        "followup_boundary_handle_receipt %s",
        json.dumps(
            receipt,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
        extra=receipt,
    )


class MainBoundaryMarkerError(ValueError):
    """A marker is untrusted and must be handled as an unmarked request."""


class MainBoundaryDispatchUncertain(RuntimeError):
    """The gate may have crossed the Dashboard RPC boundary."""


class MainBoundaryDispatchResult(str, Enum):
    """Closed result returned by the durable Run gate."""

    RELEASE = "release"
    REDIRECTED = "redirected"
    REJECTED = "rejected"
    QUEUED = "queued"


class MainBoundaryAction(str, Enum):
    FORWARD = "forward"
    ABORT = "abort"
    ISOLATE = "isolate"
    REPLAY = "replay"


class ActiveRouteLike(Protocol):
    route_token: str
    run_id: str
    session_id: str


class MainBoundaryGate(Protocol):
    async def observe_model_request(
        self,
        *,
        binding: MainBoundaryRouteBinding,
        request_body: bytes,
    ) -> bool: ...

    async def dispatch(
        self,
        handle: MainBoundaryHandle,
    ) -> MainBoundaryDispatchResult: ...


@dataclass(frozen=True, slots=True)
class MainBoundaryBoot:
    dashboard_boot_id: str
    marker_key: bytes
    boundary_epoch_nonce: str

    def __post_init__(self) -> None:
        if not self.dashboard_boot_id:
            raise ValueError("dashboard_boot_id cannot be empty")
        if len(self.marker_key) != 32:
            raise ValueError("marker_key must contain exactly 256 bits")
        _decode_base64url(
            self.boundary_epoch_nonce,
            expected_bytes=16,
            label="boundary_epoch_nonce",
        )


@dataclass(frozen=True, slots=True)
class MainBoundaryRouteBinding:
    route_token: str
    run_id: str
    dashboard_boot_id: str
    dashboard_connection_id: str
    stored_session_id: str
    live_session_id: str
    task_id: str
    profile_key: str
    boundary_epoch_nonce: str

    def __post_init__(self) -> None:
        if _ROUTE_TOKEN_RE.fullmatch(self.route_token) is None:
            raise ValueError(
                "route_token must be 64 lowercase hexadecimal characters"
            )
        for name, value in (
            ("run_id", self.run_id),
            ("dashboard_boot_id", self.dashboard_boot_id),
            ("dashboard_connection_id", self.dashboard_connection_id),
            ("stored_session_id", self.stored_session_id),
            ("live_session_id", self.live_session_id),
            ("task_id", self.task_id),
            ("profile_key", self.profile_key),
        ):
            if not value:
                raise ValueError(f"{name} cannot be empty")
        _decode_base64url(
            self.boundary_epoch_nonce,
            expected_bytes=16,
            label="boundary_epoch_nonce",
        )


@dataclass(frozen=True, slots=True)
class PlatformMainBoundaryMarkerV1:
    route_token: str
    session_id: str
    task_id: str
    turn_id: str
    api_request_id: str
    api_call_count: int
    boundary_epoch_nonce: str
    request_nonce: str
    issued_at: datetime
    expires_at: datetime
    canonical_payload: bytes
    header: str
    marker_digest: str


class MainBoundaryRouteResolver(Protocol):
    async def resolve_route(
        self,
        *,
        route: ActiveRouteLike,
        marker: PlatformMainBoundaryMarkerV1,
    ) -> MainBoundaryRouteBinding | None: ...


@dataclass(frozen=True, slots=True)
class MainBoundaryDecision:
    action: MainBoundaryAction
    disposition: str
    replay: bool = False
    _record_key: tuple[str, str, int] | None = None
    _authority: object = _DECISION_AUTHORITY


class MainBoundaryHandle:
    """One request-bound capability accepted by the Dashboard control peer."""

    __slots__ = (
        "runtime_run_id",
        "stored_session_id",
        "live_session_id",
        "dashboard_boot_id",
        "dashboard_connection_id",
        "route_token",
        "turn_id",
        "task_id",
        "api_request_id",
        "api_call_count",
        "marker_digest",
        "boundary_epoch_nonce",
        "profile_key",
        "request_nonce",
        "_authority",
        "_claimed",
    )

    def __init__(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        live_session_id: str,
        dashboard_boot_id: str,
        dashboard_connection_id: str,
        route_token: str,
        turn_id: str,
        task_id: str,
        api_request_id: str,
        api_call_count: int,
        marker_digest: str,
        boundary_epoch_nonce: str,
        profile_key: str,
        request_nonce: str,
        authority: object,
    ) -> None:
        self.runtime_run_id = runtime_run_id
        self.stored_session_id = stored_session_id
        self.live_session_id = live_session_id
        self.dashboard_boot_id = dashboard_boot_id
        self.dashboard_connection_id = dashboard_connection_id
        self.route_token = route_token
        self.turn_id = turn_id
        self.task_id = task_id
        self.api_request_id = api_request_id
        self.api_call_count = api_call_count
        self.marker_digest = marker_digest
        self.boundary_epoch_nonce = boundary_epoch_nonce
        self.profile_key = profile_key
        self.request_nonce = request_nonce
        self._authority = authority
        self._claimed = False

    @property
    def claimed(self) -> bool:
        return self._claimed

    def claim_dashboard_redirect(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        live_session_id: str,
        dashboard_connection_id: str,
    ) -> bool:
        """Consume this capability immediately before the official RPC send."""

        if self._authority is not _BOUNDARY_HANDLE_AUTHORITY or self._claimed:
            return False
        if (
            runtime_run_id != self.runtime_run_id
            or stored_session_id != self.stored_session_id
            or live_session_id != self.live_session_id
            or dashboard_connection_id != self.dashboard_connection_id
        ):
            return False
        self._claimed = True
        return True


@dataclass(slots=True)
class _StartEpoch:
    stored_session_id: str
    live_session_id: str
    dashboard_connection_id: str
    event: asyncio.Event
    conflicted: bool = False


@dataclass(slots=True)
class _RequestRecord:
    key: tuple[str, str, int]
    marker: PlatformMainBoundaryMarkerV1
    marker_digest: str
    run_id: str
    state: str
    decision: asyncio.Future[MainBoundaryDecision]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _base64url_no_pad(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(
    value: object,
    *,
    expected_bytes: int | None,
    label: str,
) -> bytes:
    if not isinstance(value, str) or _BASE64URL_RE.fullmatch(value) is None:
        raise MainBoundaryMarkerError(f"{label} must be unpadded base64url")
    try:
        decoded = base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (TypeError, ValueError) as exc:
        raise MainBoundaryMarkerError(
            f"{label} must be unpadded base64url"
        ) from exc
    if _base64url_no_pad(decoded) != value:
        raise MainBoundaryMarkerError(f"{label} is not canonical base64url")
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise MainBoundaryMarkerError(f"{label} has an invalid length")
    return decoded


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if (
        not isinstance(value, str)
        or _RFC3339_MILLISECONDS_RE.fullmatch(value) is None
    ):
        raise MainBoundaryMarkerError(
            f"{label} must be UTC RFC3339 milliseconds"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise MainBoundaryMarkerError(
            f"{label} must be UTC RFC3339 milliseconds"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _parse_marker_wire(header: str) -> PlatformMainBoundaryMarkerV1:
    try:
        header_bytes = header.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MainBoundaryMarkerError("marker header must be ASCII") from exc
    if not header_bytes or len(header_bytes) > MAIN_BOUNDARY_HEADER_MAX_BYTES:
        raise MainBoundaryMarkerError("marker header has an invalid size")
    if header.count(".") != 1:
        raise MainBoundaryMarkerError("marker header is malformed")
    encoded_payload, signature = header.split(".", 1)
    if _HEX_SHA256_RE.fullmatch(signature) is None:
        raise MainBoundaryMarkerError("marker signature is malformed")
    canonical_payload = _decode_base64url(
        encoded_payload,
        expected_bytes=None,
        label="marker payload",
    )
    try:
        payload = json.loads(canonical_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MainBoundaryMarkerError("marker payload is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _MARKER_FIELDS:
        raise MainBoundaryMarkerError("marker payload fields do not match contract")
    if _canonical_json(payload) != canonical_payload:
        raise MainBoundaryMarkerError("marker payload is not canonical JCS")
    if payload.get("contract_version") != 1:
        raise MainBoundaryMarkerError("marker contract version mismatch")
    route_token = payload.get("route_token")
    if not isinstance(route_token, str) or _ROUTE_TOKEN_RE.fullmatch(route_token) is None:
        raise MainBoundaryMarkerError("marker route token is invalid")
    identities: dict[str, str] = {}
    for field in ("session_id", "task_id", "turn_id", "api_request_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise MainBoundaryMarkerError(f"marker {field} is invalid")
        identities[field] = value
    api_call_count = payload.get("api_call_count")
    if (
        isinstance(api_call_count, bool)
        or not isinstance(api_call_count, int)
        or api_call_count < 0
        or api_call_count > _MAX_JCS_SAFE_INTEGER
    ):
        raise MainBoundaryMarkerError("marker api_call_count is invalid")
    boundary_epoch_nonce = payload.get("boundary_epoch_nonce")
    request_nonce = payload.get("request_nonce")
    _decode_base64url(
        boundary_epoch_nonce,
        expected_bytes=16,
        label="boundary_epoch_nonce",
    )
    _decode_base64url(
        request_nonce,
        expected_bytes=16,
        label="request_nonce",
    )
    issued_at = _parse_timestamp(payload.get("issued_at"), label="issued_at")
    expires_at = _parse_timestamp(payload.get("expires_at"), label="expires_at")
    return PlatformMainBoundaryMarkerV1(
        route_token=route_token,
        session_id=identities["session_id"],
        task_id=identities["task_id"],
        turn_id=identities["turn_id"],
        api_request_id=identities["api_request_id"],
        api_call_count=api_call_count,
        boundary_epoch_nonce=str(boundary_epoch_nonce),
        request_nonce=str(request_nonce),
        issued_at=issued_at,
        expires_at=expires_at,
        canonical_payload=canonical_payload,
        header=header,
        marker_digest=hashlib.sha256(header_bytes).hexdigest(),
    )


class MainBoundaryCoordinator:
    """In-memory marker verifier and main-turn pre-forward join skeleton.

    Durable follow-up admission, history anchors, event fences, and delivery
    confirmation stay
    behind ``MainBoundaryGate``.  This class owns only current-boot request
    identity and the short HTTP/WS ordering gap.
    """

    def __init__(
        self,
        *,
        gate: MainBoundaryGate,
        route_resolver: MainBoundaryRouteResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        join_timeout_seconds: float = 0.5,
        gate_timeout_seconds: float = 0.5,
    ) -> None:
        self._gate = gate
        self._route_resolver = route_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._join_timeout_seconds = max(0.001, float(join_timeout_seconds))
        self._gate_timeout_seconds = max(0.001, float(gate_timeout_seconds))
        self._boots: dict[str, MainBoundaryBoot] = {}
        self._routes: dict[str, MainBoundaryRouteBinding] = {}
        self._route_events: dict[str, asyncio.Event] = {}
        self._requests: dict[tuple[str, str, int], _RequestRecord] = {}
        self._starts: dict[str, _StartEpoch] = {}
        self._start_events: dict[str, asyncio.Event] = {}
        self._bound_turns: dict[str, tuple[str, str, str]] = {}
        self._waiting_runs: set[str] = set()
        self._lock = asyncio.Lock()

    async def register_boot(self, boot: MainBoundaryBoot) -> None:
        async with self._lock:
            existing = self._boots.get(boot.dashboard_boot_id)
            if existing is not None and existing != boot:
                raise ValueError("dashboard boot identity is already registered")
            self._boots[boot.dashboard_boot_id] = boot

    async def revoke_boot(self, dashboard_boot_id: str) -> None:
        async with self._lock:
            self._boots.pop(dashboard_boot_id, None)
            removed_routes = {
                token
                for token, binding in self._routes.items()
                if binding.dashboard_boot_id == dashboard_boot_id
            }
            for token in removed_routes:
                self._routes.pop(token, None)
                route_event = self._route_events.pop(token, None)
                if route_event is not None:
                    route_event.set()
            for key, record in tuple(self._requests.items()):
                if key[0] != dashboard_boot_id:
                    continue
                self._requests.pop(key, None)
                if not record.decision.done():
                    record.decision.set_result(
                        self._decision(
                            MainBoundaryAction.FORWARD,
                            "boot_revoked",
                            record=record,
                        )
                    )
            for event in self._start_events.values():
                event.set()
            for event in tuple(self._route_events.values()):
                event.set()
            self._route_events.clear()

    async def register_route(self, binding: MainBoundaryRouteBinding) -> None:
        async with self._lock:
            boot = self._boots.get(binding.dashboard_boot_id)
            if boot is None:
                raise ValueError("dashboard boot is not registered")
            if boot.boundary_epoch_nonce != binding.boundary_epoch_nonce:
                raise ValueError("route boundary epoch does not match dashboard boot")
            existing = self._routes.get(binding.route_token)
            if existing is not None and existing != binding:
                raise ValueError("route token is already bound")
            self._routes[binding.route_token] = binding
            route_event = self._route_events.pop(binding.route_token, None)
            if route_event is not None:
                route_event.set()

    async def revoke_route(self, route_token: str, *, run_id: str) -> bool:
        async with self._lock:
            binding = self._routes.get(route_token)
            if binding is None or binding.run_id != run_id:
                return False
            self._routes.pop(route_token, None)
            route_event = self._route_events.pop(route_token, None)
            if route_event is not None:
                route_event.set()
            return True

    async def arm_redirect(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id cannot be empty")
        async with self._lock:
            self._waiting_runs.add(run_id)
            self._start_events.setdefault(run_id, asyncio.Event())

    async def observe_control_peer_registered(
        self,
        *,
        run_id: str,
        stored_session_id: str,
        dashboard_connection_id: str,
    ) -> None:
        """Start a fresh join epoch for one exact Dashboard connection."""

        if not run_id or not stored_session_id or not dashboard_connection_id:
            return
        async with self._lock:
            for route_token, binding in tuple(self._routes.items()):
                if (
                    binding.run_id == run_id
                    and binding.dashboard_connection_id
                    != dashboard_connection_id
                ):
                    self._routes.pop(route_token, None)
                    route_event = self._route_events.pop(route_token, None)
                    if route_event is not None:
                        route_event.set()
            self._starts.pop(run_id, None)
            self._bound_turns.pop(run_id, None)
            previous = self._start_events.get(run_id)
            if previous is not None:
                previous.set()
            self._start_events[run_id] = asyncio.Event()

    async def disarm_redirect(self, run_id: str) -> None:
        async with self._lock:
            self._waiting_runs.discard(run_id)

    async def has_waiting_redirect(self, run_id: str) -> bool:
        async with self._lock:
            return run_id in self._waiting_runs

    async def observe_message_start(
        self,
        *,
        run_id: str,
        stored_session_id: str,
        live_session_id: str,
        dashboard_connection_id: str,
    ) -> None:
        if not all(
            (run_id, stored_session_id, live_session_id, dashboard_connection_id)
        ):
            return
        async with self._lock:
            for route_token, binding in tuple(self._routes.items()):
                if binding.run_id == run_id and (
                    binding.dashboard_connection_id != dashboard_connection_id
                    or binding.live_session_id != live_session_id
                ):
                    self._routes.pop(route_token, None)
                    route_event = self._route_events.pop(
                        route_token,
                        None,
                    )
                    if route_event is not None:
                        route_event.set()
            existing = self._starts.get(run_id)
            if existing is None:
                event = self._start_events.setdefault(run_id, asyncio.Event())
                existing = _StartEpoch(
                    stored_session_id=stored_session_id,
                    live_session_id=live_session_id,
                    dashboard_connection_id=dashboard_connection_id,
                    event=event,
                )
                self._starts[run_id] = existing
            elif (
                existing.stored_session_id != stored_session_id
                or existing.live_session_id != live_session_id
                or existing.dashboard_connection_id != dashboard_connection_id
            ):
                existing.conflicted = True
            # 0.20.1 may emit duplicate empty message.start frames.  Reusing
            # the same epoch is the only safe interpretation.
            existing.event.set()

    async def finish_turn(self, run_id: str) -> None:
        async with self._lock:
            self._starts.pop(run_id, None)
            self._bound_turns.pop(run_id, None)
            self._start_events[run_id] = asyncio.Event()

    async def before_forward(
        self,
        *,
        route: ActiveRouteLike,
        marker_header: str | None,
        request_body: bytes | None = None,
    ) -> MainBoundaryDecision:
        if not marker_header:
            return self._decision(MainBoundaryAction.FORWARD, "unmarked")
        try:
            marker = _parse_marker_wire(marker_header)
        except MainBoundaryMarkerError:
            return self._decision(MainBoundaryAction.FORWARD, "invalid_marker")

        loop = asyncio.get_running_loop()
        join_deadline = loop.time() + self._join_timeout_seconds
        binding, binding_error = await self._resolve_binding(
            route,
            marker,
            deadline=join_deadline,
        )
        if binding is None:
            return self._decision(
                MainBoundaryAction.FORWARD,
                binding_error,
            )

        wait_for_existing: asyncio.Future[MainBoundaryDecision] | None = None
        start_event: asyncio.Event | None = None
        async with self._lock:
            current_binding = self._routes.get(route.route_token)
            if current_binding != binding or not self._route_matches(
                route,
                current_binding,
                marker,
            ):
                return self._decision(
                    MainBoundaryAction.FORWARD,
                    "untrusted_binding",
                )
            assert binding is not None
            boot = self._boots.get(binding.dashboard_boot_id)
            if boot is None:
                return self._decision(MainBoundaryAction.FORWARD, "old_boot")
            record_key = (
                binding.dashboard_boot_id,
                marker.api_request_id,
                marker.api_call_count,
            )
            existing = self._requests.get(record_key)
            if existing is not None:
                if existing.marker_digest != marker.marker_digest:
                    return self._decision(
                        MainBoundaryAction.FORWARD,
                        "conflicting_retry",
                    )
                wait_for_existing = existing.decision
            else:
                if not self._marker_is_fresh_and_authenticated(
                    marker,
                    binding=binding,
                    boot=boot,
                ):
                    return self._decision(
                        MainBoundaryAction.FORWARD,
                        "invalid_marker",
                    )
                decision_future: asyncio.Future[MainBoundaryDecision] = (
                    asyncio.get_running_loop().create_future()
                )
                record = _RequestRecord(
                    key=record_key,
                    marker=marker,
                    marker_digest=marker.marker_digest,
                    run_id=binding.run_id,
                    state="provisional_ingress",
                    decision=decision_future,
                )
                self._requests[record_key] = record
                waiting = binding.run_id in self._waiting_runs
                if waiting and not self._start_matches(binding):
                    start_event = self._start_events.setdefault(
                        binding.run_id,
                        asyncio.Event(),
                    )

        if wait_for_existing is not None:
            first = await asyncio.shield(wait_for_existing)
            async with self._lock:
                current = self._requests.get(record_key)
                state = current.state if current is not None else first.disposition
            if first.action is MainBoundaryAction.ABORT:
                return self._decision(
                    MainBoundaryAction.ABORT,
                    state,
                    replay=True,
                )
            if first.action is MainBoundaryAction.ISOLATE:
                return self._decision(
                    MainBoundaryAction.ISOLATE,
                    state,
                    replay=True,
                )
            return self._decision(
                MainBoundaryAction.REPLAY,
                state,
                replay=True,
            )

        if start_event is not None:
            remaining = join_deadline - loop.time()
            if remaining <= 0:
                return await self._finalize_forward(record, "join_timeout")
            try:
                await asyncio.wait_for(
                    start_event.wait(),
                    timeout=remaining,
                )
            except (TimeoutError, asyncio.TimeoutError):
                return await self._finalize_forward(record, "join_timeout")

        async with self._lock:
            current_binding = self._routes.get(route.route_token)
            if (
                current_binding != binding
                or not self._start_matches(binding)
                or not self._bind_turn(binding, marker)
            ):
                return self._finalize_forward_locked(record, "start_conflict")
            if binding.run_id not in self._waiting_runs:
                decision = self._finalize_forward_locked(record, "forwarding")
            else:
                # Hermes accepts session.redirect only while its model request
                # is active. Let the provider request cross that boundary
                # first; mark_upstream_active() dispatches the durable
                # correction before any response bytes are returned to Hermes.
                decision = self._finalize_forward_locked(
                    record,
                    "awaiting_upstream_active",
                )

        # Observe content only after the signed marker, immutable route
        # binding, and current Dashboard start epoch have all been validated.
        if request_body is not None:
            try:
                await self._gate.observe_model_request(
                    binding=binding,
                    request_body=request_body,
                )
            except Exception:
                logger.exception(
                    "main boundary model request observation failed",
                    extra={
                        "event": "followup_boundary_model_request_observation_failed",
                        "parent_run_id": binding.run_id,
                    },
                )
        return decision

    async def _resolve_binding(
        self,
        route: ActiveRouteLike,
        marker: PlatformMainBoundaryMarkerV1,
        *,
        deadline: float,
    ) -> tuple[MainBoundaryRouteBinding | None, str]:
        """Resolve one authenticated route during the bounded HTTP/WS join.

        The task identity exists only inside the signed middleware marker and
        the Dashboard connection identity exists only after the stream peer is
        registered.  Consequently an exact binding cannot be assembled at
        model-route activation time.  Authenticate the marker against the
        current boot first, then allow the resolver (or a concurrent explicit
        registrar) at most the normal join window to publish that exact tuple.
        """

        async with self._lock:
            binding = self._routes.get(route.route_token)
            if binding is not None:
                if self._route_matches(route, binding, marker):
                    return binding, "bound"
                if (
                    binding.run_id == route.run_id
                    and binding.stored_session_id == route.session_id
                ):
                    return None, "untrusted_binding"
            if not self._unbound_route_matches(route, marker):
                return None, "untrusted_binding"
            boot = self._boot_for_marker_locked(marker)
            if boot is None or not self._marker_is_fresh_and_signed(
                marker,
                boot=boot,
            ):
                return None, "invalid_marker"
            if binding is not None:
                self._routes.pop(route.route_token, None)
            route_event = self._route_events.setdefault(
                route.route_token,
                asyncio.Event(),
            )
            resolver = self._route_resolver

        resolver_task: asyncio.Task[MainBoundaryRouteBinding | None] | None = None
        event_task = asyncio.create_task(route_event.wait())
        if resolver is not None:
            resolver_task = asyncio.create_task(
                resolver.resolve_route(route=route, marker=marker)
            )
        pending: set[asyncio.Task[object]] = {
            event_task,
            *((resolver_task,) if resolver_task is not None else ()),
        }
        loop = asyncio.get_running_loop()
        try:
            while pending:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                if resolver_task is not None and resolver_task in done:
                    try:
                        candidate = resolver_task.result()
                    except Exception:
                        candidate = None
                    if candidate is not None:
                        try:
                            await self.register_route(candidate)
                        except ValueError:
                            pass
                    resolver_task = None
                async with self._lock:
                    binding = self._routes.get(route.route_token)
                    if binding is not None:
                        if self._route_matches(route, binding, marker):
                            return binding, "bound"
                        return None, "untrusted_binding"
                if event_task in done:
                    break
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            async with self._lock:
                if (
                    self._routes.get(route.route_token) is None
                    and self._route_events.get(route.route_token) is route_event
                ):
                    self._route_events.pop(route.route_token, None)
        return None, "route_join_timeout"

    async def mark_upstream_active(
        self,
        decision: MainBoundaryDecision,
    ) -> MainBoundaryDecision:
        run_id: str | None = None
        async with self._lock:
            record = self._record_for_decision(decision)
            if record is not None and decision.action is MainBoundaryAction.FORWARD:
                record.state = "upstream_active"
                if record.run_id in self._waiting_runs:
                    run_id = record.run_id
        if run_id is None:
            return decision

        handle = await self.take_active_upstream_handle(run_id)
        if handle is None:
            return decision
        try:
            result = await asyncio.wait_for(
                self._gate.dispatch(handle),
                timeout=self._gate_timeout_seconds,
            )
        except (TimeoutError, asyncio.TimeoutError):
            if not handle.claimed:
                return await self._active_dispatch_forward(
                    decision,
                    "gate_timeout",
                )
            return await self._active_dispatch_isolate(
                decision,
                "rpc_timeout",
            )
        except MainBoundaryDispatchUncertain:
            return await self._active_dispatch_isolate(
                decision,
                "rpc_uncertain",
            )
        except Exception:
            if not handle.claimed:
                return await self._active_dispatch_forward(
                    decision,
                    "gate_error",
                )
            return await self._active_dispatch_isolate(
                decision,
                "rpc_uncertain",
            )

        if not isinstance(result, MainBoundaryDispatchResult):
            return await self._active_dispatch_isolate(
                decision,
                "unknown_disposition",
            )
        if result is MainBoundaryDispatchResult.RELEASE:
            if not handle.claimed:
                return await self._active_dispatch_forward(
                    decision,
                    "released",
                )
            return await self._active_dispatch_isolate(
                decision,
                "claimed_without_disposition",
            )
        if not handle.claimed:
            return await self._active_dispatch_isolate(
                decision,
                "unproven_rpc_result",
            )

        async with self._lock:
            record = self._record_for_decision(decision)
            if record is None:
                return decision
            self._waiting_runs.discard(record.run_id)
            if result is MainBoundaryDispatchResult.REDIRECTED:
                record.state = "aborted"
                return self._decision(
                    MainBoundaryAction.ABORT,
                    result.value,
                    record=record,
                )
            if result is MainBoundaryDispatchResult.REJECTED:
                record.state = "forwarding"
                return self._decision(
                    MainBoundaryAction.FORWARD,
                    result.value,
                    record=record,
                )
            record.state = "isolated"
            return self._decision(
                MainBoundaryAction.ISOLATE,
                result.value,
                record=record,
            )

    async def _active_dispatch_forward(
        self,
        decision: MainBoundaryDecision,
        disposition: str,
    ) -> MainBoundaryDecision:
        async with self._lock:
            record = self._record_for_decision(decision)
            if record is None:
                return decision
            record.state = "forwarding"
            return self._decision(
                MainBoundaryAction.FORWARD,
                disposition,
                record=record,
            )

    async def _active_dispatch_isolate(
        self,
        decision: MainBoundaryDecision,
        disposition: str,
    ) -> MainBoundaryDecision:
        async with self._lock:
            record = self._record_for_decision(decision)
            if record is None:
                return decision
            self._waiting_runs.discard(record.run_id)
            record.state = "isolated"
            return self._decision(
                MainBoundaryAction.ISOLATE,
                disposition,
                record=record,
            )

    async def take_active_upstream_handle(
        self,
        run_id: str,
    ) -> MainBoundaryHandle | None:
        """Issue the one-shot capability for the Run's current main request."""

        async with self._lock:
            candidates = [
                record
                for record in self._requests.values()
                if record.run_id == run_id and record.state == "upstream_active"
            ]
            if len(candidates) != 1:
                _log_handle_receipt(
                    "active_handle_unavailable",
                    run_id,
                    followup_candidate_count=len(candidates),
                    followup_request_states=sorted(
                        record.state
                        for record in self._requests.values()
                        if record.run_id == run_id
                    ),
                    followup_route_count=sum(
                        1
                        for binding in self._routes.values()
                        if binding.run_id == run_id
                    ),
                    followup_has_start_epoch=run_id in self._starts,
                )
                return None
            record = candidates[0]
            marker = record.marker
            binding = self._routes.get(marker.route_token)
            checks = {
                "binding_present": binding is not None,
                "run_matches": binding is not None and binding.run_id == run_id,
                "boot_matches": (
                    binding is not None
                    and binding.dashboard_boot_id == record.key[0]
                ),
                "stored_session_matches": (
                    binding is not None
                    and binding.stored_session_id == marker.session_id
                ),
                "task_matches": (
                    binding is not None and binding.task_id == marker.task_id
                ),
                "boundary_epoch_matches": (
                    binding is not None
                    and binding.boundary_epoch_nonce
                    == marker.boundary_epoch_nonce
                ),
                "start_epoch_matches": (
                    binding is not None and self._start_matches(binding)
                ),
            }
            if not all(checks.values()):
                _log_handle_receipt(
                    "active_handle_identity_mismatch",
                    run_id,
                    followup_candidate_count=1,
                    followup_identity_checks=checks,
                    followup_stored_session_id=(
                        binding.stored_session_id if binding is not None else None
                    ),
                    followup_live_session_id=(
                        binding.live_session_id if binding is not None else None
                    ),
                    followup_dashboard_connection_id=(
                        binding.dashboard_connection_id
                        if binding is not None
                        else None
                    ),
                )
                return None
            record.state = "redirect_capability_issued"
            _log_handle_receipt(
                "active_handle_issued",
                run_id,
                followup_candidate_count=1,
                followup_stored_session_id=binding.stored_session_id,
                followup_live_session_id=binding.live_session_id,
                followup_dashboard_boot_id=binding.dashboard_boot_id,
                followup_dashboard_connection_id=(
                    binding.dashboard_connection_id
                ),
                followup_boundary_epoch_nonce=(
                    binding.boundary_epoch_nonce
                ),
                followup_model_request_seq=marker.api_call_count,
                followup_model_request_digest=f"sha256:{marker.marker_digest}",
            )
            return MainBoundaryHandle(
                runtime_run_id=run_id,
                stored_session_id=binding.stored_session_id,
                live_session_id=binding.live_session_id,
                dashboard_boot_id=binding.dashboard_boot_id,
                dashboard_connection_id=binding.dashboard_connection_id,
                route_token=binding.route_token,
                turn_id=marker.turn_id,
                task_id=marker.task_id,
                api_request_id=marker.api_request_id,
                api_call_count=marker.api_call_count,
                marker_digest=marker.marker_digest,
                boundary_epoch_nonce=marker.boundary_epoch_nonce,
                profile_key=binding.profile_key,
                request_nonce=marker.request_nonce,
                authority=_BOUNDARY_HANDLE_AUTHORITY,
            )

    async def mark_completed(self, decision: MainBoundaryDecision) -> None:
        async with self._lock:
            record = self._record_for_decision(decision)
            if record is not None and decision.action is MainBoundaryAction.FORWARD:
                record.state = "completed"

    async def request_disposition(self, marker_header: str) -> str | None:
        try:
            marker = _parse_marker_wire(marker_header)
        except MainBoundaryMarkerError:
            return None
        async with self._lock:
            for key, record in self._requests.items():
                if (
                    key[1] == marker.api_request_id
                    and key[2] == marker.api_call_count
                    and record.marker_digest == marker.marker_digest
                ):
                    return record.state
        return None

    @staticmethod
    def sign_test_marker(key: bytes, payload: dict[str, Any]) -> str:
        """Build deterministic fixture wire; production signing stays in overlay."""

        encoded = _canonical_json(payload)
        return (
            f"{_base64url_no_pad(encoded)}."
            f"{hmac.new(key, encoded, hashlib.sha256).hexdigest()}"
        )

    def _marker_is_fresh_and_authenticated(
        self,
        marker: PlatformMainBoundaryMarkerV1,
        *,
        binding: MainBoundaryRouteBinding,
        boot: MainBoundaryBoot,
    ) -> bool:
        if (
            marker.boundary_epoch_nonce != binding.boundary_epoch_nonce
            or marker.boundary_epoch_nonce != boot.boundary_epoch_nonce
        ):
            return False
        return self._marker_is_fresh_and_signed(marker, boot=boot)

    def _marker_is_fresh_and_signed(
        self,
        marker: PlatformMainBoundaryMarkerV1,
        *,
        boot: MainBoundaryBoot,
    ) -> bool:
        if marker.boundary_epoch_nonce != boot.boundary_epoch_nonce:
            return False
        expected = hmac.new(
            boot.marker_key,
            marker.canonical_payload,
            hashlib.sha256,
        ).hexdigest()
        signature = marker.header.rsplit(".", 1)[1]
        if not hmac.compare_digest(expected, signature):
            return False
        lifetime = (marker.expires_at - marker.issued_at).total_seconds()
        if not 0 < lifetime <= MAIN_BOUNDARY_MAX_TTL_SECONDS:
            return False
        now = self._clock()
        if now.tzinfo is None:
            raise RuntimeError("main boundary clock must be timezone-aware")
        normalized_now = now.astimezone(timezone.utc)
        skew = MAIN_BOUNDARY_CLOCK_SKEW_SECONDS
        return (
            normalized_now >= marker.issued_at - timedelta(seconds=skew)
            and normalized_now <= marker.expires_at + timedelta(seconds=skew)
        )

    def _boot_for_marker_locked(
        self,
        marker: PlatformMainBoundaryMarkerV1,
    ) -> MainBoundaryBoot | None:
        matches = [
            boot
            for boot in self._boots.values()
            if boot.boundary_epoch_nonce == marker.boundary_epoch_nonce
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _unbound_route_matches(
        route: ActiveRouteLike,
        marker: PlatformMainBoundaryMarkerV1,
    ) -> bool:
        return (
            bool(route.run_id)
            and route.route_token == marker.route_token
            and route.session_id == marker.session_id
        )

    @staticmethod
    def _route_matches(
        route: ActiveRouteLike,
        binding: MainBoundaryRouteBinding | None,
        marker: PlatformMainBoundaryMarkerV1,
    ) -> bool:
        return (
            binding is not None
            and route.route_token == binding.route_token == marker.route_token
            and route.run_id == binding.run_id
            and route.session_id == binding.stored_session_id
            and marker.session_id == binding.stored_session_id
            and marker.task_id == binding.task_id
        )

    def _start_matches(self, binding: MainBoundaryRouteBinding) -> bool:
        start = self._starts.get(binding.run_id)
        return (
            start is not None
            and not start.conflicted
            and start.stored_session_id == binding.stored_session_id
            and start.live_session_id == binding.live_session_id
            and start.dashboard_connection_id == binding.dashboard_connection_id
        )

    def _bind_turn(
        self,
        binding: MainBoundaryRouteBinding,
        marker: PlatformMainBoundaryMarkerV1,
    ) -> bool:
        identity = (marker.turn_id, marker.task_id, marker.session_id)
        existing = self._bound_turns.get(binding.run_id)
        if existing is None:
            self._bound_turns[binding.run_id] = identity
            return True
        return existing == identity

    async def _finalize_forward(
        self,
        record: _RequestRecord,
        disposition: str,
    ) -> MainBoundaryDecision:
        async with self._lock:
            return self._finalize_forward_locked(record, disposition)

    def _finalize_forward_locked(
        self,
        record: _RequestRecord,
        disposition: str,
    ) -> MainBoundaryDecision:
        record.state = "forwarding"
        decision = self._decision(
            MainBoundaryAction.FORWARD,
            disposition,
            record=record,
        )
        self._resolve_record(record, decision)
        return decision

    async def _finalize_isolate(
        self,
        record: _RequestRecord,
        disposition: str,
    ) -> MainBoundaryDecision:
        async with self._lock:
            self._waiting_runs.discard(record.run_id)
            record.state = "isolated"
            decision = self._decision(
                MainBoundaryAction.ISOLATE,
                disposition,
                record=record,
            )
            self._resolve_record(record, decision)
            return decision

    @staticmethod
    def _resolve_record(
        record: _RequestRecord,
        decision: MainBoundaryDecision,
    ) -> None:
        if not record.decision.done():
            record.decision.set_result(decision)

    @staticmethod
    def _decision(
        action: MainBoundaryAction,
        disposition: str,
        *,
        record: _RequestRecord | None = None,
        replay: bool = False,
    ) -> MainBoundaryDecision:
        return MainBoundaryDecision(
            action=action,
            disposition=disposition,
            replay=replay,
            _record_key=record.key if record is not None else None,
        )

    def _record_for_decision(
        self,
        decision: MainBoundaryDecision,
    ) -> _RequestRecord | None:
        if (
            decision._authority is not _DECISION_AUTHORITY
            or decision._record_key is None
        ):
            return None
        return self._requests.get(decision._record_key)
__all__ = [
    "MAIN_BOUNDARY_HEADER",
    "MAIN_BOUNDARY_HEADER_MAX_BYTES",
    "MainBoundaryAction",
    "MainBoundaryBoot",
    "MainBoundaryCoordinator",
    "MainBoundaryDecision",
    "MainBoundaryDispatchResult",
    "MainBoundaryDispatchUncertain",
    "MainBoundaryGate",
    "MainBoundaryHandle",
    "MainBoundaryMarkerError",
    "MainBoundaryRouteBinding",
    "MainBoundaryRouteResolver",
    "PlatformMainBoundaryMarkerV1",
]
