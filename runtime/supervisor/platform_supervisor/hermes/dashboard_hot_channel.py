from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from platform_supervisor.hermes.protocol import HermesProtocolError


def dashboard_session_workspace_path(
    root: str | None,
    runtime_session_id: str,
    *,
    runtime_agent_profile_id: int | None = None,
    hermes_profile_name: str = "default",
) -> str | None:
    if not root:
        return None
    safe_session_id = re.sub(r"[^A-Za-z0-9._-]", "_", runtime_session_id)
    base = root.rstrip("/")
    if hermes_profile_name != "default" and runtime_agent_profile_id is not None:
        base = f"{base}/experts/{int(runtime_agent_profile_id)}"
    return f"{base}/sessions/{safe_session_id}"


class DashboardWebSocketLike(Protocol):
    async def send(self, value: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class DashboardResumeRequired(HermesProtocolError):
    """A prompt may have reached Hermes and must be recovered, never resubmitted."""


class DashboardPromptSubmitTimeout(HermesProtocolError):
    """Prompt submission ACK did not arrive within the bounded RPC window."""


class DashboardChannelBusy(HermesProtocolError):
    """The Session already has an in-process HotChannel run lease."""


class DashboardHotCapacityUnavailable(HermesProtocolError):
    """No evictable READY Application HotChannel slot is available."""


class DashboardPeerDisconnected(HermesProtocolError):
    pass


class DashboardRedirectUncertain(HermesProtocolError):
    """The official redirect RPC crossed, or may have crossed, its send boundary."""


class DashboardSteerUncertain(HermesProtocolError):
    """The official steer RPC crossed, or may have crossed, its send boundary."""


class DashboardRedirectDisposition(str, Enum):
    REDIRECTED = "redirected"
    REJECTED = "rejected"
    QUEUED = "queued"


@dataclass(frozen=True, slots=True)
class DashboardRedirectResult:
    disposition: DashboardRedirectDisposition
    raw_result: dict[str, Any]

    @property
    def accepted(self) -> bool:
        return self.disposition is DashboardRedirectDisposition.REDIRECTED

    @property
    def uncertain(self) -> bool:
        return self.disposition is DashboardRedirectDisposition.QUEUED


@dataclass(frozen=True, slots=True)
class DashboardInterruptSettleResult:
    interrupt_sent: bool
    interrupt_result: dict[str, Any] | None
    checkpoint: dict[str, Any]


class _DashboardPrewarmSuperseded(HermesProtocolError):
    """The selected Session changed while a selected-only prewarm was running."""


class HotChannelState(str, Enum):
    CONNECTING = "connecting"
    WARMING = "warming"
    READY = "ready"
    IN_USE = "in_use"
    DRAINING = "draining"
    BROKEN = "broken"
    CLOSED = "closed"


class SubmitState(str, Enum):
    NOT_SENT = "not_sent"
    SEND_ATTEMPTED = "send_attempted"
    ACKNOWLEDGED = "acknowledged"


@dataclass(frozen=True)
class DashboardHotChannelIdentity:
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_session_db_id: int
    runtime_session_id: str
    runtime_id: str
    gateway_model_id: str
    config_generation: int
    endpoint_digest: str
    credential_digest: str
    workspace_path_digest: str
    effective_model_entry_id: int
    model_policy_digest: str
    coordination_scope_key: str | None = None
    independent_session: bool = False
    reuse_across_turns: bool = False
    max_coordination_slots: int = 1

    @property
    def coordination_scope(self) -> str:
        explicit = str(self.coordination_scope_key or "").strip()
        if explicit:
            return explicit
        return f"runtime-workspace:{int(self.runtime_workspace_id)}"

    @property
    def scope(self) -> tuple[int, int, str, str]:
        return (
            self.org_id,
            self.user_id,
            self.coordination_scope,
            self.runtime_session_id,
        )

    @property
    def owner_scope(self) -> tuple[int, int, str]:
        return (self.org_id, self.user_id, self.coordination_scope)


@dataclass(frozen=True)
class _PeerFailure:
    error: BaseException


class DashboardRpcPeer:
    """One-reader JSON-RPC peer shared by requests and event notifications."""

    def __init__(self, websocket: DashboardWebSocketLike) -> None:
        self._websocket = websocket
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_methods: dict[str, str] = {}
        self._notifications: asyncio.Queue[dict[str, Any] | _PeerFailure] = asyncio.Queue()
        self._recent_notifications: deque[dict[str, Any]] = deque(maxlen=32)
        self._notification_observers: set[
            Callable[[dict[str, Any]], None]
        ] = set()
        self._notification_waiters: dict[
            asyncio.Future[dict[str, Any]],
            Callable[[dict[str, Any]], bool],
        ] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if self._closed:
            raise DashboardPeerDisconnected("runtime dashboard peer is closed")
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_forever())

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise DashboardPeerDisconnected("runtime dashboard peer is closed")
        await self.start()
        request_id = f"platform-{uuid4().hex}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._pending_methods[request_id] = method
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            async with self._write_lock:
                await self._websocket.send(json.dumps(frame, ensure_ascii=False))
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(future, timeout=max(0.1, timeout_seconds))
        except (TimeoutError, asyncio.TimeoutError) as exc:
            error_type = (
                DashboardPromptSubmitTimeout
                if method == "prompt.submit"
                else HermesProtocolError
            )
            raise error_type(
                f"runtime dashboard RPC {method} timed out after "
                f"{timeout_seconds:.1f}s",
                rpc_method=method,
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            self._pending_methods.pop(request_id, None)

    def add_notification_observer(
        self,
        observer: Callable[[dict[str, Any]], None],
    ) -> None:
        self._notification_observers.add(observer)
        for frame in tuple(self._recent_notifications):
            try:
                observer(frame)
            except Exception:
                continue

    def remove_notification_observer(
        self,
        observer: Callable[[dict[str, Any]], None],
    ) -> None:
        self._notification_observers.discard(observer)

    async def next_notification(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        await self.start()
        if timeout_seconds is None:
            value = await self._notifications.get()
        else:
            value = await asyncio.wait_for(
                self._notifications.get(), timeout=max(0.1, timeout_seconds)
            )
        if isinstance(value, _PeerFailure):
            raise DashboardPeerDisconnected(str(value.error) or type(value.error).__name__)
        return value

    async def wait_for_notification(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Observe a notification without removing it from the stream queue."""
        if self._closed:
            raise DashboardPeerDisconnected("runtime dashboard peer is closed")
        await self.start()
        for frame in tuple(self._recent_notifications):
            if predicate(frame):
                return frame
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._notification_waiters[future] = predicate
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(
                future,
                timeout=max(0.1, timeout_seconds),
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise HermesProtocolError(
                "runtime dashboard readiness notification timed out"
            ) from exc
        finally:
            self._notification_waiters.pop(future, None)

    def discard_buffered_notifications(self) -> int:
        """Drop notifications left after the previous turn's terminal event."""
        discarded = 0
        while True:
            try:
                self._notifications.get_nowait()
            except asyncio.QueueEmpty:
                return discarded
            discarded += 1

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        error = DashboardPeerDisconnected("runtime dashboard peer closed")
        self._fail_pending(error)
        self._notification_observers.clear()

    async def _read_forever(self) -> None:
        try:
            while True:
                frame = self._decode(await self._websocket.recv())
                request_id = str(frame.get("id") or "")
                future = self._pending.get(request_id) if request_id else None
                if future is not None:
                    self._resolve(
                        future,
                        frame,
                        rpc_method=self._pending_methods.get(request_id),
                    )
                elif frame.get("method") == "event":
                    self._recent_notifications.append(frame)
                    for observer in tuple(self._notification_observers):
                        try:
                            observer(frame)
                        except Exception:
                            continue
                    for waiter, predicate in tuple(
                        self._notification_waiters.items()
                    ):
                        if waiter.done():
                            self._notification_waiters.pop(waiter, None)
                            continue
                        try:
                            matches = predicate(frame)
                        except Exception:
                            matches = False
                        if matches:
                            waiter.set_result(frame)
                            self._notification_waiters.pop(waiter, None)
                    self._notifications.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._closed = True
            self._fail_pending(exc)
            self._notifications.put_nowait(_PeerFailure(exc))

    @staticmethod
    def _decode(raw: str | bytes) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            frame = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardPeerDisconnected("runtime dashboard returned invalid JSON") from exc
        if not isinstance(frame, dict):
            raise DashboardPeerDisconnected("runtime dashboard returned a non-object frame")
        return frame

    @staticmethod
    def _resolve(
        future: asyncio.Future[dict[str, Any]],
        frame: dict[str, Any],
        *,
        rpc_method: str | None,
    ) -> None:
        if future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(
                HermesProtocolError(
                    str(error.get("message") or "runtime dashboard RPC failed"),
                    rpc_code=(
                        error.get("code")
                        if isinstance(error.get("code"), int)
                        and not isinstance(error.get("code"), bool)
                        else None
                    ),
                    rpc_method=rpc_method,
                    rpc_data=error.get("data"),
                )
            )
            return
        result = frame.get("result")
        if not isinstance(result, dict):
            future.set_exception(
                HermesProtocolError(
                    "runtime dashboard RPC returned invalid result",
                    rpc_method=rpc_method,
                )
            )
            return
        future.set_result(result)

    def _fail_pending(self, error: BaseException) -> None:
        wrapped = (
            error
            if isinstance(error, HermesProtocolError)
            else DashboardPeerDisconnected(str(error) or type(error).__name__)
        )
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(wrapped)
        for future in tuple(self._notification_waiters):
            if not future.done():
                future.set_exception(wrapped)


class DashboardRedirectBoundaryHandle(Protocol):
    live_session_id: str

    def claim_dashboard_redirect(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        live_session_id: str,
        dashboard_connection_id: str,
    ) -> bool: ...


class DashboardMainBoundaryObserver(Protocol):
    async def observe_control_peer_registered(
        self,
        *,
        run_id: str,
        stored_session_id: str,
        dashboard_connection_id: str,
    ) -> None: ...

    async def observe_message_start(
        self,
        *,
        run_id: str,
        stored_session_id: str,
        live_session_id: str,
        dashboard_connection_id: str,
    ) -> None: ...

    async def observe_tool_activity(
        self,
        *,
        run_id: str,
        active: bool,
    ) -> None: ...


@dataclass(slots=True)
class _ActiveRunControlPeerSlot:
    runtime_run_id: str
    stored_session_id: str
    dashboard_connection_id: str
    peer: DashboardRpcPeer
    live_session_id: str | None = None
    agent_ready: bool = False
    active_tool_calls: set[str] = field(default_factory=set)
    live_session_event: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ActiveRunControlPeerLease:
    def __init__(
        self,
        registry: ActiveRunControlPeerRegistry,
        slot: _ActiveRunControlPeerSlot,
    ) -> None:
        self._registry = registry
        self._slot = slot
        self._closed = False

    @property
    def runtime_run_id(self) -> str:
        return self._slot.runtime_run_id

    @property
    def stored_session_id(self) -> str:
        return self._slot.stored_session_id

    @property
    def dashboard_connection_id(self) -> str:
        return self._slot.dashboard_connection_id

    @property
    def live_session_id(self) -> str | None:
        return self._slot.live_session_id

    @property
    def peer(self) -> DashboardRpcPeer:
        return self._slot.peer

    async def bind_live_session(self, live_session_id: str) -> None:
        await self._registry._bind_live_session(self._slot, live_session_id)

    async def observe_message_start(self) -> None:
        await self._registry._observe_message_start(self._slot)

    async def observe_tool_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        await self._registry._observe_tool_event(self._slot, event_type, payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._registry._unregister(self._slot)


class ActiveRunControlPeerRegistry:
    """Exact Run-scoped access to each stream's existing one-reader peer.

    Registration never creates a second Dashboard WebSocket.  Both HotChannel
    and cold/fallback streams publish their already-owned peer for the exact
    connection lifetime and unregister by connection identity.
    """

    def __init__(
        self,
        *,
        boundary_observer: DashboardMainBoundaryObserver | None = None,
    ) -> None:
        self._boundary_observer = boundary_observer
        self._slots: dict[str, _ActiveRunControlPeerSlot] = {}
        self._connection_events: dict[str, asyncio.Event] = {}
        self._guard = asyncio.Lock()

    def set_boundary_observer(
        self,
        observer: DashboardMainBoundaryObserver | None,
    ) -> None:
        self._boundary_observer = observer

    async def register(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        peer: DashboardRpcPeer,
    ) -> ActiveRunControlPeerLease:
        if not runtime_run_id or not stored_session_id:
            raise ValueError("control peer Run and Session identities are required")
        slot = _ActiveRunControlPeerSlot(
            runtime_run_id=runtime_run_id,
            stored_session_id=stored_session_id,
            dashboard_connection_id=f"dashboard-{uuid4().hex}",
            peer=peer,
        )
        async with self._guard:
            existing = self._slots.get(runtime_run_id)
            if existing is not None and not existing.closed:
                raise DashboardChannelBusy(
                    "runtime Run already owns a Dashboard control peer"
                )
            self._slots[runtime_run_id] = slot
            connection_event = self._connection_events.pop(
                runtime_run_id,
                None,
            )
            if connection_event is not None:
                connection_event.set()
            observer = self._boundary_observer
        if observer is not None:
            try:
                await observer.observe_control_peer_registered(
                    run_id=runtime_run_id,
                    stored_session_id=stored_session_id,
                    dashboard_connection_id=slot.dashboard_connection_id,
                )
            except BaseException:
                async with self._guard:
                    if self._slots.get(runtime_run_id) is slot:
                        self._slots.pop(runtime_run_id, None)
                    slot.closed = True
                raise
        return ActiveRunControlPeerLease(self, slot)

    async def current_connection_id(self, runtime_run_id: str) -> str | None:
        async with self._guard:
            slot = self._slots.get(runtime_run_id)
            if slot is None or slot.closed:
                return None
            return slot.dashboard_connection_id

    async def wait_for_connection(self, runtime_run_id: str) -> str:
        """Wait for the exact Run peer without creating another WebSocket.

        The caller owns its timeout.  Registration wakes all current joiners;
        cancellation leaves only a reusable, Run-scoped event behind.
        """

        if not runtime_run_id:
            raise ValueError("runtime_run_id cannot be empty")
        while True:
            async with self._guard:
                slot = self._slots.get(runtime_run_id)
                if slot is not None and not slot.closed:
                    return slot.dashboard_connection_id
                connection_event = self._connection_events.setdefault(
                    runtime_run_id,
                    asyncio.Event(),
                )
            await connection_event.wait()

    async def wait_for_route_identity(
        self,
        runtime_run_id: str,
    ) -> tuple[str, str]:
        """Wait until one exact Run peer has also bound its live Session."""

        if not runtime_run_id:
            raise ValueError("runtime_run_id cannot be empty")
        while True:
            async with self._guard:
                slot = self._slots.get(runtime_run_id)
                if slot is not None and not slot.closed:
                    if slot.live_session_id:
                        return (
                            slot.dashboard_connection_id,
                            slot.live_session_id,
                        )
                    ready = slot.live_session_event
                else:
                    ready = self._connection_events.setdefault(
                        runtime_run_id,
                        asyncio.Event(),
                    )
            await ready.wait()

    async def is_agent_ready(self, runtime_run_id: str) -> bool:
        async with self._guard:
            slot = self._slots.get(runtime_run_id)
            return bool(
                slot is not None
                and not slot.closed
                and slot.agent_ready
                and not slot.peer.closed
            )

    async def is_tool_active(self, runtime_run_id: str) -> bool:
        async with self._guard:
            slot = self._slots.get(runtime_run_id)
            return bool(
                slot is not None
                and not slot.closed
                and slot.agent_ready
                and slot.active_tool_calls
                and not slot.peer.closed
            )

    async def steer(
        self,
        *,
        runtime_run_id: str,
        wire_text: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not wire_text or wire_text != wire_text.strip():
            raise HermesProtocolError(
                "runtime dashboard steer wire text must be non-empty and stripped",
                rpc_method="session.steer",
            )
        async with self._guard:
            slot = self._slots.get(runtime_run_id)
        if slot is None or slot.closed:
            raise HermesProtocolError(
                "runtime Run has no active Dashboard control peer",
                rpc_method="session.steer",
            )
        async with slot.control_lock:
            async with self._guard:
                current = self._slots.get(runtime_run_id)
                if current is not slot or slot.closed:
                    raise HermesProtocolError(
                        "runtime Dashboard control peer was superseded",
                        rpc_method="session.steer",
                    )
                tool_active = bool(slot.active_tool_calls)
            if not slot.agent_ready or not tool_active or slot.peer.closed:
                raise HermesProtocolError(
                    "runtime Dashboard peer is not executing a tool",
                    rpc_method="session.steer",
                )
            live_session_id = slot.live_session_id or slot.stored_session_id
            try:
                result = await slot.peer.request(
                    "session.steer",
                    {"session_id": live_session_id, "text": wire_text},
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                raise DashboardSteerUncertain(
                    "runtime dashboard session.steer disposition is uncertain",
                    rpc_method="session.steer",
                ) from exc
            if str(result.get("status") or "").strip().lower() != "queued":
                raise DashboardSteerUncertain(
                    "runtime dashboard returned an unsupported session.steer disposition",
                    rpc_method="session.steer",
                    rpc_data={"status": result.get("status")},
                )
            return result

    async def redirect(
        self,
        *,
        runtime_run_id: str,
        wire_text: str,
        boundary_handle: DashboardRedirectBoundaryHandle,
        timeout_seconds: float | None = None,
    ) -> DashboardRedirectResult:
        if not wire_text or wire_text != wire_text.strip():
            raise HermesProtocolError(
                "runtime dashboard redirect wire text must be non-empty and stripped",
                rpc_method="session.redirect",
            )
        async with self._guard:
            slot = self._slots.get(runtime_run_id)
        if slot is None or slot.closed:
            raise HermesProtocolError(
                "runtime Run has no active Dashboard control peer",
                rpc_method="session.redirect",
            )
        async with slot.control_lock:
            async with self._guard:
                current = self._slots.get(runtime_run_id)
                if current is not slot or slot.closed:
                    raise HermesProtocolError(
                        "runtime Dashboard control peer was superseded",
                        rpc_method="session.redirect",
                    )
            if not slot.agent_ready:
                raise HermesProtocolError(
                    "runtime Dashboard control peer is not agent-ready",
                    rpc_method="session.redirect",
                )
            if slot.peer.closed:
                raise HermesProtocolError(
                    "runtime Dashboard control peer is disconnected",
                    rpc_method="session.redirect",
                )
            live_session_id = slot.live_session_id
            if not live_session_id:
                raise HermesProtocolError(
                    "runtime Dashboard control peer has no bound live Session",
                    rpc_method="session.redirect",
                )
            claim = getattr(boundary_handle, "claim_dashboard_redirect", None)
            if not callable(claim) or not claim(
                runtime_run_id=runtime_run_id,
                stored_session_id=slot.stored_session_id,
                live_session_id=live_session_id,
                dashboard_connection_id=slot.dashboard_connection_id,
            ):
                raise HermesProtocolError(
                    "runtime Dashboard redirect requires a valid main-boundary handle",
                    rpc_method="session.redirect",
                )
            try:
                result = await slot.peer.request(
                    "session.redirect",
                    {
                        "session_id": boundary_handle.live_session_id,
                        "text": wire_text,
                    },
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                details: dict[str, Any] = {}
                if isinstance(exc, HermesProtocolError):
                    details = {
                        "rpc_code": exc.rpc_code,
                        "rpc_method": "session.redirect",
                        "rpc_data": exc.rpc_data,
                        "status_code": exc.status_code,
                        "path": exc.path,
                    }
                raise DashboardRedirectUncertain(
                    "runtime dashboard session.redirect disposition is uncertain",
                    **details,
                ) from exc
            raw_status = str(result.get("status") or "").strip().lower()
            try:
                disposition = DashboardRedirectDisposition(raw_status)
            except ValueError as exc:
                raise DashboardRedirectUncertain(
                    "runtime dashboard returned an unsupported session.redirect disposition",
                    rpc_method="session.redirect",
                    rpc_data={"status": raw_status or None},
                ) from exc
            return DashboardRedirectResult(
                disposition=disposition,
                raw_result=result,
            )

    async def interrupt_and_settle_redirect(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        profile_name: str = "default",
        timeout_seconds: float | None = None,
    ) -> DashboardInterruptSettleResult:
        """Interrupt once and prove idle on the Run's existing single reader."""

        del profile_name
        async with self._guard:
            slot = self._slots.get(runtime_run_id)
        if slot is None or slot.closed:
            raise HermesProtocolError(
                "runtime Run has no active Dashboard recovery peer",
                rpc_method="session.interrupt",
            )
        if slot.stored_session_id != stored_session_id:
            raise HermesProtocolError(
                "runtime Dashboard recovery Session identity conflicts",
                rpc_method="session.interrupt",
            )
        deadline = time.monotonic() + (
            30.0 if timeout_seconds is None else max(0.1, timeout_seconds)
        )
        async with slot.control_lock:
            async with self._guard:
                current = self._slots.get(runtime_run_id)
                if current is not slot or slot.closed:
                    raise HermesProtocolError(
                        "runtime Dashboard recovery peer was superseded",
                        rpc_method="session.interrupt",
                    )
            if slot.peer.closed:
                raise HermesProtocolError(
                    "runtime Dashboard recovery peer is disconnected",
                    rpc_method="session.interrupt",
                )
            live_session_id = slot.live_session_id or slot.stored_session_id
            interrupt_result = await slot.peer.request(
                "session.interrupt",
                {"session_id": live_session_id},
                timeout_seconds=self._remaining(deadline),
            )
            while True:
                checkpoint = await slot.peer.request(
                    "session.resume",
                    {
                        "session_id": live_session_id,
                        "omit_messages": True,
                        "cols": 96,
                    },
                    timeout_seconds=self._remaining(deadline),
                )
                if self.redirect_checkpoint_is_settled(checkpoint):
                    return DashboardInterruptSettleResult(
                        interrupt_sent=True,
                        interrupt_result=interrupt_result,
                        checkpoint=checkpoint,
                    )
                remaining = self._remaining(deadline)
                await asyncio.sleep(min(0.1, remaining))

    @staticmethod
    def redirect_checkpoint_is_settled(checkpoint: dict[str, Any]) -> bool:
        """Require explicit idle/running=false and empty in-memory work."""

        info = checkpoint.get("info")
        status = str(checkpoint.get("status") or "").strip().lower()
        if not status and isinstance(info, dict):
            status = str(info.get("status") or "").strip().lower()
        running: object = checkpoint.get("running")
        if not isinstance(running, bool) and isinstance(info, dict):
            running = info.get("running")
        usage = checkpoint.get("usage")
        if not isinstance(usage, dict) and isinstance(info, dict):
            usage = info.get("usage")
        active_subagents = (
            usage.get("active_subagents", 0)
            if isinstance(usage, dict)
            else 0
        )
        if isinstance(active_subagents, bool) or not isinstance(
            active_subagents,
            (int, float),
        ):
            return False
        return bool(
            status == "idle"
            and running is False
            and not checkpoint.get("inflight")
            and not checkpoint.get("queued")
            and not checkpoint.get("queued_prompt")
            and active_subagents == 0
        )

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HermesProtocolError(
                "runtime Dashboard redirect Session did not settle",
                rpc_method="session.resume",
            )
        return remaining

    async def _bind_live_session(
        self,
        slot: _ActiveRunControlPeerSlot,
        live_session_id: str,
    ) -> None:
        if not live_session_id:
            raise ValueError("live_session_id cannot be empty")
        async with self._guard:
            if self._slots.get(slot.runtime_run_id) is not slot or slot.closed:
                raise HermesProtocolError(
                    "runtime Dashboard control peer is no longer active"
                )
            slot.live_session_id = live_session_id
            slot.live_session_event.set()

    async def _observe_message_start(
        self,
        slot: _ActiveRunControlPeerSlot,
    ) -> None:
        async with self._guard:
            if self._slots.get(slot.runtime_run_id) is not slot or slot.closed:
                return
            if not slot.live_session_id:
                raise HermesProtocolError(
                    "runtime Dashboard message.start has no bound live Session"
                )
            slot.agent_ready = True
            live_session_id = slot.live_session_id
            observer = self._boundary_observer
        if observer is not None:
            await observer.observe_message_start(
                run_id=slot.runtime_run_id,
                stored_session_id=slot.stored_session_id,
                live_session_id=live_session_id,
                dashboard_connection_id=slot.dashboard_connection_id,
            )

    async def _observe_tool_event(
        self,
        slot: _ActiveRunControlPeerSlot,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        raw_call_id = (
            payload.get("tool_call_id")
            or payload.get("call_id")
            or payload.get("id")
            or "__active_tool__"
        )
        call_id = str(raw_call_id)
        async with self._guard:
            if self._slots.get(slot.runtime_run_id) is not slot or slot.closed:
                return
            if event_type == "tool.start":
                slot.active_tool_calls.add(call_id)
            elif event_type in {"tool.complete", "tool.error"}:
                slot.active_tool_calls.discard(call_id)
            active = bool(slot.active_tool_calls)
            observer = self._boundary_observer
        callback = getattr(observer, "observe_tool_activity", None)
        if callable(callback):
            await callback(run_id=slot.runtime_run_id, active=active)

    async def _unregister(self, slot: _ActiveRunControlPeerSlot) -> None:
        async with slot.control_lock:
            async with self._guard:
                if self._slots.get(slot.runtime_run_id) is slot:
                    self._slots.pop(slot.runtime_run_id, None)
                slot.closed = True
                slot.live_session_event.set()


class DashboardRpcPeerSocket:
    """Compatibility socket for the existing request/event translation loop.

    The wrapper never reads the underlying WebSocket. It multiplexes the
    peer's already-dispatched notifications with synthetic JSON-RPC responses
    so legacy `_request(send/recv)` code can remain unchanged.
    """

    def __init__(
        self,
        peer: DashboardRpcPeer,
        *,
        lease: DashboardHotRunLease | None = None,
        start_immediately: bool = True,
    ) -> None:
        self._peer = peer
        self._lease = lease
        self._frames: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._notification_task: asyncio.Task[None] | None = None
        if start_immediately:
            self._start_notification_pump()
        self._closed = False

    async def send(self, value: str) -> None:
        if self._closed:
            raise DashboardPeerDisconnected("runtime dashboard peer socket is closed")
        try:
            frame = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HermesProtocolError("runtime dashboard request is invalid JSON") from exc
        if not isinstance(frame, dict):
            raise HermesProtocolError("runtime dashboard request is not an object")
        request_id = str(frame.get("id") or "")
        method = frame.get("method")
        params = frame.get("params")
        if not request_id or not isinstance(method, str) or not isinstance(params, dict):
            raise HermesProtocolError("runtime dashboard request is invalid")
        task = asyncio.create_task(self._call(request_id, method, params))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._start_notification_pump()

    async def recv(self) -> str:
        value = await self._frames.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        notification_task = self._notification_task
        self._notification_task = None
        if notification_task is not None:
            notification_task.cancel()
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(
            *(() if notification_task is None else (notification_task,)),
            *self._tasks,
            return_exceptions=True,
        )
        self._tasks.clear()

    def _start_notification_pump(self) -> None:
        if self._notification_task is None:
            self._notification_task = asyncio.create_task(
                self._pump_notifications()
            )

    async def _call(self, request_id: str, method: str, params: dict[str, Any]) -> None:
        try:
            if method == "prompt.submit" and self._lease is not None:
                result = await self._lease.submit(params)
            elif self._lease is not None:
                result = await self._lease.request(method, params)
            else:
                result = await self._peer.request(method, params)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except asyncio.CancelledError:
            raise
        except (
            DashboardPromptSubmitTimeout,
            DashboardResumeRequired,
            DashboardPeerDisconnected,
        ) as exc:
            self._frames.put_nowait(exc)
            return
        except Exception as exc:
            error: dict[str, Any] = {
                "message": str(exc) or type(exc).__name__,
            }
            if isinstance(exc, HermesProtocolError):
                if exc.rpc_code is not None:
                    error["code"] = exc.rpc_code
                if exc.rpc_data is not None:
                    error["data"] = exc.rpc_data
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": error,
            }
        self._frames.put_nowait(json.dumps(response, ensure_ascii=False))

    async def _pump_notifications(self) -> None:
        try:
            while True:
                frame = await self._peer.next_notification()
                self._frames.put_nowait(json.dumps(frame, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._frames.put_nowait(exc)


@dataclass(frozen=True)
class DashboardPreparedPeer:
    live_session_id: str
    workspace: str | None
    model: str | None = None
    provider: str | None = None
    ready: bool = True


PreparePeer = Callable[
    [DashboardRpcPeer],
    Awaitable[DashboardPreparedPeer | tuple[str, str | None]],
]
WaitReadyPeer = Callable[[DashboardRpcPeer, str], Awaitable[None]]


@dataclass
class _HotChannelSlot:
    identity: DashboardHotChannelIdentity
    peer: DashboardRpcPeer
    connection_context: Any
    live_session_id: str
    workspace: str | None
    model: str | None
    provider: str | None
    last_business_touch: float
    endpoint: Any
    connector: Callable[[Any], Any]
    prepare: PreparePeer
    wait_ready: WaitReadyPeer | None
    epoch: int
    state: HotChannelState = HotChannelState.READY
    active_runtime_run_id: str | None = None
    submit_state: SubmitState = SubmitState.NOT_SENT
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    ready_error: BaseException | None = None
    ready_task: asyncio.Task[None] | None = None
    reclaim_observer: Callable[[dict[str, Any]], None] | None = None
    reclaim_task: asyncio.Task[None] | None = None
    reclaimed: bool = False
    closed: bool = False


class DashboardHotRunLease:
    def __init__(
        self,
        manager: DashboardHotChannelManager,
        slot: _HotChannelSlot,
        runtime_run_id: str,
    ) -> None:
        self._manager = manager
        self._slot = slot
        self.runtime_run_id = runtime_run_id
        self._released = False
        self._clean_terminal = False

    @property
    def clean_terminal(self) -> bool:
        return self._clean_terminal

    def mark_clean_terminal(self) -> None:
        self._clean_terminal = True

    @property
    def peer(self) -> DashboardRpcPeer:
        return self._slot.peer

    @property
    def live_session_id(self) -> str:
        return self._slot.live_session_id

    @property
    def workspace(self) -> str | None:
        return self._slot.workspace

    @property
    def model(self) -> str | None:
        return self._slot.model

    @property
    def provider(self) -> str | None:
        return self._slot.provider

    @property
    def state(self) -> HotChannelState:
        return self._slot.state

    @property
    def submit_state(self) -> SubmitState:
        return self._slot.submit_state

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._slot.peer.request(method, params, timeout_seconds=timeout_seconds)

    async def submit(
        self,
        params: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._slot.submit_state = SubmitState.SEND_ATTEMPTED
        try:
            result = await self._slot.peer.request(
                "prompt.submit", params, timeout_seconds=timeout_seconds
            )
        except DashboardPromptSubmitTimeout:
            raise
        except Exception as exc:
            details: dict[str, Any] = {}
            if isinstance(exc, HermesProtocolError):
                details = {
                    "rpc_code": exc.rpc_code,
                    "rpc_method": exc.rpc_method,
                    "rpc_data": exc.rpc_data,
                    "status_code": exc.status_code,
                    "path": exc.path,
                }
            raise DashboardResumeRequired(
                str(exc) or type(exc).__name__,
                **details,
            ) from exc
        self._slot.submit_state = SubmitState.ACKNOWLEDGED
        return result

    async def next_notification(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._slot.peer.next_notification(timeout_seconds=timeout_seconds)

    async def release(self, *, terminal: bool) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release(self._slot, self.runtime_run_id, terminal=terminal)

    async def __aenter__(self) -> DashboardHotRunLease:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release(terminal=exc is None)


class DashboardHotChannelManager:
    def __init__(
        self,
        *,
        idle_grace_seconds: float,
        now: Callable[[], float] | None = None,
        max_independent_slots: int = 128,
    ) -> None:
        self._idle_grace_seconds = max(1.0, float(idle_grace_seconds))
        self._now = now or time.monotonic
        self._max_independent_slots = max(
            1,
            min(1024, int(max_independent_slots)),
        )
        self._guard = asyncio.Lock()
        self._slots: dict[DashboardHotChannelIdentity, _HotChannelSlot] = {}
        self._current: dict[
            tuple[int, int, str, str],
            DashboardHotChannelIdentity,
        ] = {}
        self._inflight: dict[DashboardHotChannelIdentity, asyncio.Task[_HotChannelSlot]] = {}
        self._epochs: dict[tuple[int, int, str, str], int] = {}
        self._selected: dict[tuple[int, int, str], str] = {}
        self._selection_epochs: dict[tuple[int, int, str], int] = {}
        self._workspace_operation_locks: dict[str, asyncio.Lock] = {}
        self._cold_run_counts: Counter[str] = Counter()
        self._closed = False
        self._reaper_task: asyncio.Task[None] | None = None
        self._results: Counter[str] = Counter()

    @property
    def slot_count(self) -> int:
        return len(self._slots)

    @property
    def ready_count(self) -> int:
        return sum(slot.state is HotChannelState.READY for slot in self._slots.values())

    @property
    def warming_count(self) -> int:
        return sum(
            slot.state is HotChannelState.WARMING for slot in self._slots.values()
        )

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    def result_snapshot(self) -> dict[str, int]:
        return dict(self._results)

    @staticmethod
    def _coordination_scope(value: int | str) -> str:
        if isinstance(value, bool):
            raise ValueError("coordination scope must not be boolean")
        if isinstance(value, int):
            if value <= 0:
                raise ValueError("runtime_workspace_id must be positive")
            return f"runtime-workspace:{value}"
        scope = str(value or "").strip()
        if not scope:
            raise ValueError("coordination scope must not be empty")
        return scope

    def workspace_operation(self, scope: int | str) -> asyncio.Lock:
        coordination_scope = self._coordination_scope(scope)
        lock = self._workspace_operation_locks.get(coordination_scope)
        if lock is None:
            lock = asyncio.Lock()
            self._workspace_operation_locks[coordination_scope] = lock
        return lock

    async def has_blocking_owner_channel(
        self,
        identity: DashboardHotChannelIdentity,
    ) -> bool:
        async with self._guard:
            return any(
                current_identity.owner_scope == identity.owner_scope
                and current_identity != identity
                and not (
                    current_identity.independent_session
                    and identity.independent_session
                )
                and slot.state
                in {
                    HotChannelState.IN_USE,
                    HotChannelState.DRAINING,
                }
                for current_identity, slot in self._slots.items()
            )

    async def require_workspace_idle(self, scope: int | str) -> None:
        coordination_scope = self._coordination_scope(scope)
        async with self._guard:
            busy = (
                self._cold_run_counts[coordination_scope] > 0
                or any(
                    identity.coordination_scope == coordination_scope
                    and slot.state
                    in {HotChannelState.IN_USE, HotChannelState.DRAINING}
                    for identity, slot in self._slots.items()
                )
            )
        if busy:
            raise DashboardChannelBusy(
                "runtime dashboard workspace has an active run"
            )

    @asynccontextmanager
    async def cold_run(self, scope: int | str) -> AsyncIterator[None]:
        coordination_scope = self._coordination_scope(scope)
        async with self.workspace_operation(coordination_scope):
            async with self._guard:
                if self._closed:
                    raise HermesProtocolError(
                        "runtime dashboard HotChannel manager is closed"
                    )
                self._cold_run_counts[coordination_scope] += 1
        try:
            yield
        finally:
            async with self._guard:
                remaining = self._cold_run_counts[coordination_scope] - 1
                if remaining > 0:
                    self._cold_run_counts[coordination_scope] = remaining
                else:
                    self._cold_run_counts.pop(coordination_scope, None)

    async def select_session(
        self,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> int:
        owner_scope = (
            org_id,
            user_id,
            self._coordination_scope(runtime_workspace_id),
        )
        to_close: list[_HotChannelSlot] = []
        to_cancel: list[asyncio.Task[_HotChannelSlot]] = []
        async with self._guard:
            if self._closed:
                raise HermesProtocolError("runtime dashboard HotChannel manager is closed")
            if self._selected.get(owner_scope) == runtime_session_id:
                return self._selection_epochs.get(owner_scope, 0)
            self._selected[owner_scope] = runtime_session_id
            selection_epoch = self._selection_epochs.get(owner_scope, 0) + 1
            self._selection_epochs[owner_scope] = selection_epoch
            stale_scopes = {
                identity.scope
                for identity in (*self._slots.keys(), *self._inflight.keys())
                if identity.owner_scope == owner_scope
                and identity.runtime_session_id != runtime_session_id
                and not identity.independent_session
            }
            for scope in stale_scopes:
                self._epochs[scope] = self._epochs.get(scope, 0) + 1
                self._current.pop(scope, None)
            for identity, task in tuple(self._inflight.items()):
                if (
                    identity.scope in stale_scopes
                    and self._inflight.pop(identity, None) is task
                ):
                    task.cancel()
                    to_cancel.append(task)
            for identity, slot in tuple(self._slots.items()):
                if identity.scope not in stale_scopes:
                    continue
                if slot.state is HotChannelState.IN_USE:
                    slot.state = HotChannelState.DRAINING
                else:
                    if slot.state is HotChannelState.WARMING:
                        slot.ready_error = _DashboardPrewarmSuperseded(
                            "runtime dashboard Session prewarm was superseded"
                        )
                        slot.ready_event.set()
                    self._remove_slot_locked(identity, slot)
                    slot.state = HotChannelState.CLOSED
                    to_close.append(slot)
        await asyncio.gather(*to_cancel, return_exceptions=True)
        await asyncio.gather(
            *(self._close_slot(slot) for slot in to_close),
            return_exceptions=True,
        )
        return selection_epoch

    async def prewarm(
        self,
        identity: DashboardHotChannelIdentity,
        endpoint: Any,
        connector: Callable[[Any], Any],
        prepare: PreparePeer,
        *,
        wait_ready: WaitReadyPeer | None = None,
        selected_only: bool = False,
        selection_epoch: int | None = None,
    ) -> bool:
        async with self.workspace_operation(identity.coordination_scope):
            if not selected_only and not identity.independent_session:
                await self.select_session(
                    identity.org_id,
                    identity.user_id,
                    identity.runtime_workspace_id,
                    identity.runtime_session_id,
                )
            if not identity.independent_session:
                if not await self._wait_for_single_owner_channel(
                    identity,
                    selected_only=selected_only,
                    selection_epoch=selection_epoch,
                ):
                    self._results["prewarm_superseded"] += 1
                    return False
            slot = await self._ensure(
                identity,
                endpoint,
                connector,
                prepare,
                wait_ready,
                selected_only=selected_only,
                selection_epoch=selection_epoch,
            )
        if slot is None:
            return False
        try:
            await self._wait_slot_ready(slot)
        except _DashboardPrewarmSuperseded:
            if not selected_only:
                raise
            self._results["prewarm_superseded"] += 1
            return False
        if selected_only:
            async with self._guard:
                if not self._selection_matches_locked(
                    identity,
                    selection_epoch,
                ):
                    self._results["prewarm_superseded"] += 1
                    return False
                reusable = (
                    self._slots.get(identity) is slot
                    and self._current.get(identity.scope) == identity
                    and self._epochs.get(identity.scope, 0) == slot.epoch
                    and slot.state
                    in {HotChannelState.READY, HotChannelState.IN_USE}
                    and not slot.peer.closed
                    and not slot.reclaimed
                )
            if not reusable:
                raise HermesProtocolError(
                    "runtime dashboard HotChannel is not reusable after prewarm"
                )
        slot.last_business_touch = self._now()
        return True

    async def _wait_for_single_owner_channel(
        self,
        identity: DashboardHotChannelIdentity,
        *,
        selected_only: bool,
        selection_epoch: int | None,
    ) -> bool:
        """Never create a new Session channel while the previous one drains."""
        while True:
            async with self._guard:
                if self._closed:
                    raise HermesProtocolError(
                        "runtime dashboard HotChannel manager is closed"
                    )
                if (
                    selected_only
                    and not self._selection_matches_locked(
                        identity,
                        selection_epoch,
                    )
                ):
                    return False
                blocked = any(
                    current_identity.owner_scope == identity.owner_scope
                    and current_identity != identity
                    and slot.state
                    in {
                        HotChannelState.IN_USE,
                        HotChannelState.DRAINING,
                    }
                    for current_identity, slot in self._slots.items()
                )
            if not blocked:
                return True
            await asyncio.sleep(0.01)

    async def acquire_run(
        self,
        identity: DashboardHotChannelIdentity,
        endpoint: Any,
        connector: Callable[[Any], Any],
        prepare: PreparePeer,
        *,
        wait_ready: WaitReadyPeer | None = None,
        runtime_run_id: str,
        on_result: Callable[[str], None] | None = None,
    ) -> DashboardHotRunLease:
        async with self.workspace_operation(identity.coordination_scope):
            slot = await self._ensure(
                identity,
                endpoint,
                connector,
                prepare,
                wait_ready,
                on_result=on_result,
            )
        await self._wait_slot_ready(slot)
        async with slot.lock:
            if slot.state is HotChannelState.IN_USE:
                raise DashboardChannelBusy(
                    "runtime dashboard Session already has an active run"
                )
            if (
                slot.state is not HotChannelState.READY
                or slot.identity != identity
                or slot.peer.closed
                or slot.reclaimed
            ):
                raise HermesProtocolError(
                    "runtime dashboard HotChannel is not reusable"
                )
            discarded = slot.peer.discard_buffered_notifications()
            if discarded:
                self._results["stale_notifications_discarded"] += discarded
            slot.state = HotChannelState.IN_USE
            slot.active_runtime_run_id = runtime_run_id
            slot.submit_state = SubmitState.NOT_SENT
            slot.last_business_touch = self._now()
        return DashboardHotRunLease(self, slot, runtime_run_id)

    async def touch_workspace(self, org_id: int, user_id: int, runtime_workspace_id: int) -> None:
        owner_scope = (
            org_id,
            user_id,
            self._coordination_scope(runtime_workspace_id),
        )
        now = self._now()
        async with self._guard:
            selected_session_id = self._selected.get(owner_scope)
            if selected_session_id is None:
                return
            for slot in self._slots.values():
                identity = slot.identity
                if (
                    identity.owner_scope == owner_scope
                    and identity.runtime_session_id == selected_session_id
                    and slot.state
                    in {
                        HotChannelState.WARMING,
                        HotChannelState.READY,
                        HotChannelState.IN_USE,
                    }
                    and (
                        slot.state is HotChannelState.IN_USE
                        or not slot.peer.closed
                    )
                ):
                    slot.last_business_touch = now

    async def invalidate_session(
        self,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        *,
        reason: str,
    ) -> None:
        del reason
        default_scope = (
            org_id,
            user_id,
            self._coordination_scope(runtime_workspace_id),
            runtime_session_id,
        )
        async with self._guard:
            scopes = {
                identity.scope
                for identity in (*self._slots.keys(), *self._inflight.keys())
                if identity.org_id == org_id
                and identity.user_id == user_id
                and identity.runtime_workspace_id == runtime_workspace_id
                and identity.runtime_session_id == runtime_session_id
            }
        scopes.add(default_scope)
        await self._invalidate_scopes(scopes)

    async def invalidate_workspace(
        self,
        org_id: int,
        user_id: int | None,
        runtime_workspace_id: int,
        *,
        reason: str,
    ) -> None:
        del reason
        async with self._guard:
            scopes = {
                identity.scope
                for identity in (*self._slots.keys(), *self._inflight.keys())
                if identity.org_id == org_id
                and (user_id is None or identity.user_id == user_id)
                and identity.runtime_workspace_id == runtime_workspace_id
            }
            scopes.update(
                (*owner_scope, runtime_session_id)
                for owner_scope, runtime_session_id in self._selected.items()
                if owner_scope[0] == org_id
                and (user_id is None or owner_scope[1] == user_id)
                and owner_scope[2]
                == self._coordination_scope(runtime_workspace_id)
            )
        await self._invalidate_scopes(scopes)

    async def reap_idle(self) -> None:
        now = self._now()
        to_close: list[_HotChannelSlot] = []
        async with self._guard:
            for identity, slot in tuple(self._slots.items()):
                if now - slot.last_business_touch <= self._idle_grace_seconds:
                    continue
                if slot.state is HotChannelState.IN_USE:
                    slot.state = HotChannelState.DRAINING
                    continue
                if slot.state in {
                    HotChannelState.WARMING,
                    HotChannelState.READY,
                }:
                    self._remove_slot_locked(identity, slot)
                    slot.state = HotChannelState.CLOSED
                    to_close.append(slot)
        await asyncio.gather(*(self._close_slot(slot) for slot in to_close), return_exceptions=True)

    async def close(self) -> None:
        async with self._guard:
            if self._closed and not self._slots and not self._inflight:
                return
            self._closed = True
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
            reaper_task = self._reaper_task
            self._reaper_task = None
            slots = tuple(self._slots.values())
            self._slots.clear()
            self._current.clear()
            self._selected.clear()
            self._selection_epochs.clear()
            self._workspace_operation_locks.clear()
            self._cold_run_counts.clear()
        for task in tasks:
            task.cancel()
        if reaper_task is not None:
            reaper_task.cancel()
        waiters = [*tasks]
        if reaper_task is not None:
            waiters.append(reaper_task)
        await asyncio.gather(*waiters, return_exceptions=True)
        await asyncio.gather(*(self._close_slot(slot) for slot in slots), return_exceptions=True)

    def _ensure_reaper_locked(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_forever())

    async def _reap_forever(self) -> None:
        interval = max(1.0, min(30.0, self._idle_grace_seconds / 2.0))
        try:
            while True:
                await asyncio.sleep(interval)
                await self.reap_idle()
        except asyncio.CancelledError:
            raise

    async def _ensure(
        self,
        identity: DashboardHotChannelIdentity,
        endpoint: Any,
        connector: Callable[[Any], Any],
        prepare: PreparePeer,
        wait_ready: WaitReadyPeer | None,
        on_result: Callable[[str], None] | None = None,
        *,
        selected_only: bool = False,
        selection_epoch: int | None = None,
    ) -> _HotChannelSlot | None:
        stale_to_close: list[_HotChannelSlot] = []
        capacity_unavailable = False
        task: asyncio.Task[_HotChannelSlot] | None = None
        async with self._guard:
            if self._closed:
                raise HermesProtocolError("runtime dashboard HotChannel manager is closed")
            if (
                selected_only
                and not self._selection_matches_locked(
                    identity,
                    selection_epoch,
                )
            ):
                self._results["prewarm_superseded"] += 1
                return None
            slot = self._slots.get(identity)
            if (
                slot is not None
                and slot.state
                in {HotChannelState.WARMING, HotChannelState.READY}
                and (slot.peer.closed or slot.reclaimed)
            ):
                self._results["dead_ready"] += 1
                self._remove_slot_locked(identity, slot)
                slot.state = HotChannelState.CLOSED
                stale_to_close.append(slot)
                slot = None
            if slot is not None and slot.state in {
                HotChannelState.WARMING,
                HotChannelState.READY,
                HotChannelState.IN_USE,
                HotChannelState.DRAINING,
            }:
                self._results["hit"] += 1
                if on_result is not None:
                    on_result("hit")
                return slot
            task = self._inflight.get(identity)
            if task is None:
                scope = identity.scope
                current_identity = self._current.get(scope)
                if current_identity is not None and current_identity != identity:
                    self._results["stale"] += 1
                    self._epochs[scope] = self._epochs.get(scope, 0) + 1
                    stale = self._slots.get(current_identity)
                    if stale is not None:
                        if stale.state is HotChannelState.IN_USE:
                            stale.state = HotChannelState.DRAINING
                        elif stale.state in {
                            HotChannelState.WARMING,
                            HotChannelState.READY,
                        }:
                            self._remove_slot_locked(current_identity, stale)
                            stale.state = HotChannelState.CLOSED
                            stale_to_close.append(stale)
                if identity.independent_session:
                    evicted, available = (
                        self._reserve_independent_capacity_locked(identity)
                    )
                    stale_to_close.extend(evicted)
                    capacity_unavailable = not available
                if not capacity_unavailable:
                    self._current[scope] = identity
                    epoch = self._epochs.get(scope, 0)
                    task = self._start_connect_locked(
                        identity,
                        endpoint,
                        connector,
                        prepare,
                        wait_ready,
                        epoch,
                    )
                    self._results["miss"] += 1
                    if on_result is not None:
                        on_result("miss")
            else:
                self._results["shared_connect"] += 1
                if on_result is not None:
                    on_result("shared_connect")
        if stale_to_close:
            await asyncio.gather(
                *(self._close_slot(slot) for slot in stale_to_close),
                return_exceptions=True,
            )
        if capacity_unavailable:
            self._results["capacity_cold_fallback"] += 1
            if on_result is not None:
                on_result("capacity_cold_fallback")
            raise DashboardHotCapacityUnavailable(
                "runtime dashboard Application HotChannel capacity is full"
            )
        assert task is not None
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            superseded = False
            if selected_only and task.cancelled():
                async with self._guard:
                    superseded = (
                        not self._closed
                        and not self._selection_matches_locked(
                            identity,
                            selection_epoch,
                        )
                    )
            if superseded:
                self._results["prewarm_superseded"] += 1
                return None
            raise
        except Exception as exc:
            if selected_only:
                async with self._guard:
                    superseded = not self._selection_matches_locked(
                        identity,
                        selection_epoch,
                    )
                if superseded:
                    self._results["prewarm_superseded"] += 1
                    return None
            self._results["connect_error"] += 1
            if isinstance(exc, HermesProtocolError):
                raise
            raise HermesProtocolError(str(exc) or type(exc).__name__) from exc

    def _reserve_independent_capacity_locked(
        self,
        identity: DashboardHotChannelIdentity,
    ) -> tuple[list[_HotChannelSlot], bool]:
        """Evict oldest READY Application slots until one reservation fits."""

        to_close: list[_HotChannelSlot] = []
        coordination_limit = max(
            1,
            min(self._max_independent_slots, int(identity.max_coordination_slots)),
        )
        evictable_identities = {
            candidate
            for candidate, slot in self._slots.items()
            if candidate.independent_session
            and candidate != identity
            and slot.state is HotChannelState.READY
        }
        all_independent = {
            candidate
            for candidate in (*self._slots.keys(), *self._inflight.keys())
            if candidate.independent_session and candidate != identity
        }
        non_evictable = all_independent - evictable_identities
        if (
            sum(
                candidate.coordination_scope
                == identity.coordination_scope
                for candidate in non_evictable
            )
            >= coordination_limit
            or len(non_evictable) >= self._max_independent_slots
        ):
            return to_close, False
        while True:
            active_identities = {
                candidate
                for candidate in (*self._slots.keys(), *self._inflight.keys())
                if candidate.independent_session and candidate != identity
            }
            coordination_count = sum(
                candidate.coordination_scope
                == identity.coordination_scope
                for candidate in active_identities
            )
            needs_coordination_slot = (
                coordination_count >= coordination_limit
            )
            needs_global_slot = (
                len(active_identities) >= self._max_independent_slots
            )
            if not needs_coordination_slot and not needs_global_slot:
                return to_close, True

            if needs_coordination_slot:
                candidates = [
                    slot
                    for candidate, slot in self._slots.items()
                    if candidate.independent_session
                    and candidate != identity
                    and slot.state is HotChannelState.READY
                    and candidate.coordination_scope
                    == identity.coordination_scope
                ]
            else:
                candidates = [
                    slot
                    for candidate, slot in self._slots.items()
                    if candidate.independent_session
                    and candidate != identity
                    and slot.state is HotChannelState.READY
                ]
            if not candidates:
                return to_close, False
            victim = min(
                candidates,
                key=lambda slot: slot.last_business_touch,
            )
            self._remove_slot_locked(victim.identity, victim)
            victim.state = HotChannelState.CLOSED
            to_close.append(victim)
            self._results["capacity_lru_evicted"] += 1

    def _start_connect_locked(
        self,
        identity: DashboardHotChannelIdentity,
        endpoint: Any,
        connector: Callable[[Any], Any],
        prepare: PreparePeer,
        wait_ready: WaitReadyPeer | None,
        epoch: int,
    ) -> asyncio.Task[_HotChannelSlot]:
        task = asyncio.create_task(
            self._connect(
                identity,
                endpoint,
                connector,
                prepare,
                wait_ready,
                epoch,
            )
        )
        self._inflight[identity] = task
        task.add_done_callback(self._consume_connect_result)
        self._ensure_reaper_locked()
        return task

    @staticmethod
    def _consume_connect_result(task: asyncio.Task[_HotChannelSlot]) -> None:
        if not task.cancelled():
            task.exception()

    async def _connect(
        self,
        identity: DashboardHotChannelIdentity,
        endpoint: Any,
        connector: Callable[[Any], Any],
        prepare: PreparePeer,
        wait_ready: WaitReadyPeer | None,
        epoch: int,
    ) -> _HotChannelSlot:
        context = connector(endpoint)
        peer: DashboardRpcPeer | None = None
        slot: _HotChannelSlot | None = None
        entered = False
        try:
            websocket = await context.__aenter__()
            entered = True
            peer = DashboardRpcPeer(websocket)
            await peer.start()
            prepared = await prepare(peer)
            if isinstance(prepared, DashboardPreparedPeer):
                live_session_id = prepared.live_session_id
                workspace = prepared.workspace
                model = prepared.model
                provider = prepared.provider
                ready = prepared.ready
            else:
                live_session_id, workspace = prepared
                model = None
                provider = None
                ready = True
            if not ready and wait_ready is None:
                raise HermesProtocolError(
                    "runtime dashboard warming channel has no readiness waiter"
                )
            slot = _HotChannelSlot(
                identity=identity,
                peer=peer,
                connection_context=context,
                live_session_id=live_session_id,
                workspace=workspace,
                model=model,
                provider=provider,
                last_business_touch=self._now(),
                endpoint=endpoint,
                connector=connector,
                prepare=prepare,
                wait_ready=wait_ready,
                epoch=epoch,
                state=(
                    HotChannelState.READY
                    if ready
                    else HotChannelState.WARMING
                ),
            )
            if ready:
                slot.ready_event.set()
            async with self._guard:
                if (
                    self._closed
                    or self._epochs.get(identity.scope, 0) != epoch
                    or self._current.get(identity.scope) != identity
                ):
                    raise HermesProtocolError("runtime dashboard HotChannel became stale")
                self._slots[identity] = slot
                if not ready:
                    slot.ready_task = asyncio.create_task(
                        self._finish_warm(slot)
                    )
                slot.reclaim_observer = self._reclaim_observer(slot)
                peer.add_notification_observer(slot.reclaim_observer)
            return slot
        except BaseException as exc:
            if peer is not None:
                await peer.close()
            if entered:
                await context.__aexit__(None, None, None)
            if isinstance(exc, asyncio.CancelledError):
                async with self._guard:
                    manager_closed = self._closed
                if not manager_closed:
                    raise HermesProtocolError(
                        "runtime dashboard HotChannel connect was invalidated"
                    ) from None
            raise
        finally:
            async with self._guard:
                if self._inflight.get(identity) is asyncio.current_task():
                    self._inflight.pop(identity, None)

    def _selection_matches_locked(
        self,
        identity: DashboardHotChannelIdentity,
        selection_epoch: int | None,
    ) -> bool:
        if (
            self._selected.get(identity.owner_scope)
            != identity.runtime_session_id
        ):
            return False
        return (
            selection_epoch is None
            or self._selection_epochs.get(identity.owner_scope, 0)
            == selection_epoch
        )

    async def _finish_warm(self, slot: _HotChannelSlot) -> None:
        assert slot.wait_ready is not None
        try:
            await slot.wait_ready(slot.peer, slot.live_session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            slot.ready_error = exc
            should_close = False
            async with self._guard:
                if (
                    self._slots.get(slot.identity) is slot
                    and slot.state is HotChannelState.WARMING
                ):
                    self._remove_slot_locked(slot.identity, slot)
                    slot.state = HotChannelState.BROKEN
                    should_close = True
            self._results["warm_error"] += 1
            if should_close:
                try:
                    await self._close_slot(slot)
                finally:
                    slot.ready_event.set()
            else:
                slot.ready_event.set()
            return

        async with slot.lock:
            if slot.state is HotChannelState.WARMING:
                slot.state = HotChannelState.READY
            slot.ready_event.set()
        self._results["warm_ready"] += 1

    def _reclaim_observer(
        self,
        slot: _HotChannelSlot,
    ) -> Callable[[dict[str, Any]], None]:
        def observe(frame: dict[str, Any]) -> None:
            if not self._is_matching_reclaim(slot, frame):
                return
            slot.reclaimed = True
            task = slot.reclaim_task
            if task is not None and not task.done():
                return
            task = asyncio.create_task(self._invalidate_reclaimed_slot(slot))
            slot.reclaim_task = task
            task.add_done_callback(self._consume_reclaim_result)

        return observe

    @staticmethod
    def _is_matching_reclaim(
        slot: _HotChannelSlot,
        frame: dict[str, Any],
    ) -> bool:
        params = frame.get("params")
        if not isinstance(params, dict) or params.get("type") != "session.reclaimed":
            return False
        payload = params.get("payload")
        return bool(
            isinstance(payload, dict)
            and str(payload.get("session_id") or "") == slot.live_session_id
            and str(payload.get("stored_session_id") or "")
            == slot.identity.runtime_session_id
        )

    @staticmethod
    def _consume_reclaim_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _invalidate_reclaimed_slot(self, slot: _HotChannelSlot) -> None:
        should_close = False
        async with self._guard:
            if self._slots.get(slot.identity) is not slot:
                return
            self._epochs[slot.identity.scope] = (
                self._epochs.get(slot.identity.scope, 0) + 1
            )
            self._remove_slot_locked(slot.identity, slot)
            if slot.state is HotChannelState.WARMING:
                slot.ready_error = HermesProtocolError(
                    "runtime dashboard Session was reclaimed while warming"
                )
                slot.ready_event.set()
            slot.state = HotChannelState.BROKEN
            self._results["session_reclaimed"] += 1
            should_close = True
        if should_close:
            await self._close_slot(slot)

    @staticmethod
    async def _wait_slot_ready(slot: _HotChannelSlot) -> None:
        await slot.ready_event.wait()
        if slot.ready_error is not None:
            if isinstance(slot.ready_error, HermesProtocolError):
                raise slot.ready_error
            raise HermesProtocolError(
                str(slot.ready_error) or type(slot.ready_error).__name__
            ) from slot.ready_error

    async def _release(
        self,
        slot: _HotChannelSlot,
        runtime_run_id: str,
        *,
        terminal: bool,
    ) -> None:
        async with slot.lock:
            if slot.active_runtime_run_id != runtime_run_id:
                return
            slot.active_runtime_run_id = None
            slot.submit_state = SubmitState.NOT_SENT
            self._results["run_terminal" if terminal else "run_nonterminal"] += 1

        async with self._guard:
            registered = self._slots.get(slot.identity) is slot
            can_reuse = (
                terminal
                and registered
                and slot.identity.reuse_across_turns
                and not self._closed
                and slot.state is HotChannelState.IN_USE
                and not slot.peer.closed
                and not slot.reclaimed
                and self._current.get(slot.identity.scope) == slot.identity
                and self._epochs.get(slot.identity.scope, 0) == slot.epoch
            )
            if can_reuse:
                slot.state = HotChannelState.READY
                slot.last_business_touch = self._now()
                self._results["run_reused"] += 1
                return
            if registered:
                self._slots.pop(slot.identity, None)
            can_replace = (
                terminal
                and registered
                and not self._closed
                and slot.state is HotChannelState.IN_USE
                and not slot.reclaimed
                and (
                    slot.identity.independent_session
                    or self._selected.get(slot.identity.owner_scope)
                    == slot.identity.runtime_session_id
                )
                and self._current.get(slot.identity.scope) == slot.identity
                and self._epochs.get(slot.identity.scope, 0) == slot.epoch
                and slot.identity not in self._inflight
            )
            slot.state = HotChannelState.CLOSED
            if can_replace:
                self._start_connect_locked(
                    slot.identity,
                    slot.endpoint,
                    slot.connector,
                    slot.prepare,
                    slot.wait_ready,
                    slot.epoch,
                )

        await self._close_slot(slot)

    async def _invalidate_scopes(
        self,
        scopes: set[tuple[int, int, str, str]],
    ) -> None:
        to_close: list[_HotChannelSlot] = []
        to_cancel: list[asyncio.Task[_HotChannelSlot]] = []
        async with self._guard:
            for scope in scopes:
                owner_scope = scope[:3]
                if self._selected.get(owner_scope) == scope[3]:
                    self._selection_epochs[owner_scope] = (
                        self._selection_epochs.get(owner_scope, 0) + 1
                    )
                self._epochs[scope] = self._epochs.get(scope, 0) + 1
                current = self._current.pop(scope, None)
                if current is not None:
                    task = self._inflight.pop(current, None)
                    if task is not None:
                        task.cancel()
                        to_cancel.append(task)
            for identity, slot in tuple(self._slots.items()):
                if identity.scope not in scopes:
                    continue
                if slot.state is HotChannelState.IN_USE:
                    slot.state = HotChannelState.DRAINING
                else:
                    if slot.state is HotChannelState.WARMING:
                        slot.ready_error = _DashboardPrewarmSuperseded(
                            "runtime dashboard Session prewarm was invalidated"
                        )
                        slot.ready_event.set()
                    self._remove_slot_locked(identity, slot)
                    slot.state = HotChannelState.CLOSED
                    to_close.append(slot)
        # A terminal run can immediately start its replacement channel.  Do
        # not let a session mutation continue to Dashboard live-session
        # release while that canceled prepare is still unwinding: otherwise
        # it can resume the same durable Session after active_list and leave
        # the next prepared channel bound to a live Session that is then
        # closed out from under it.
        await asyncio.gather(*to_cancel, return_exceptions=True)
        await asyncio.gather(*(self._close_slot(slot) for slot in to_close), return_exceptions=True)

    def _remove_slot_locked(
        self,
        identity: DashboardHotChannelIdentity,
        slot: _HotChannelSlot,
    ) -> None:
        if self._slots.get(identity) is slot:
            self._slots.pop(identity, None)
        if self._current.get(identity.scope) == identity:
            self._current.pop(identity.scope, None)

    @staticmethod
    async def _close_slot(slot: _HotChannelSlot) -> None:
        if slot.closed:
            return
        slot.closed = True
        observer = slot.reclaim_observer
        slot.reclaim_observer = None
        if observer is not None:
            slot.peer.remove_notification_observer(observer)
        ready_task = slot.ready_task
        slot.ready_task = None
        closing_from_ready_task = ready_task is asyncio.current_task()
        if not closing_from_ready_task and not slot.ready_event.is_set():
            if slot.ready_error is None:
                slot.ready_error = HermesProtocolError(
                    "runtime dashboard HotChannel closed before it became ready"
                )
            slot.ready_event.set()
        if (
            ready_task is not None
            and not closing_from_ready_task
            and not ready_task.done()
        ):
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
        await slot.peer.close()
        await slot.connection_context.__aexit__(None, None, None)


_shared_manager: DashboardHotChannelManager | None = None
_shared_control_peers: ActiveRunControlPeerRegistry | None = None


def shared_dashboard_hot_channel_manager(
    *,
    idle_grace_seconds: float = 900,
    max_independent_slots: int = 564,
) -> DashboardHotChannelManager:
    global _shared_manager
    if _shared_manager is None:
        _shared_manager = DashboardHotChannelManager(
            idle_grace_seconds=idle_grace_seconds,
            max_independent_slots=max_independent_slots,
        )
    return _shared_manager


def shared_active_run_control_peers() -> ActiveRunControlPeerRegistry:
    global _shared_control_peers
    if _shared_control_peers is None:
        _shared_control_peers = ActiveRunControlPeerRegistry()
    return _shared_control_peers


async def close_dashboard_hot_channels() -> None:
    global _shared_control_peers, _shared_manager
    manager = _shared_manager
    _shared_manager = None
    _shared_control_peers = None
    if manager is not None:
        await manager.close()
