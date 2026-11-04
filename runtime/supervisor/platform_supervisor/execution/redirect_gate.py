from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Protocol
from uuid import uuid4

from platform_supervisor.hermes.dashboard_hot_channel import (
    ActiveRunControlPeerRegistry,
    DashboardRedirectDisposition,
    DashboardRedirectUncertain,
    DashboardSteerUncertain,
    HermesProtocolError,
)
from platform_supervisor.model_relay.boundary import (
    ActiveRouteLike,
    MainBoundaryBoot,
    MainBoundaryCoordinator,
    MainBoundaryDispatchResult,
    MainBoundaryDispatchUncertain,
    MainBoundaryHandle,
    MainBoundaryRouteBinding,
    PlatformMainBoundaryMarkerV1,
)
from platform_supervisor.storage import (
    SQLiteStore,
    SQLiteStoreError,
    StoredRedirectDispatchGroup,
)


logger = logging.getLogger(__name__)
REDIRECT_CARRIER_PROOF_SECONDS = 30.0
REDIRECT_SESSION_ISOLATION_SECONDS = 30.0


def _log_boundary_receipt(
    stage: str,
    group: StoredRedirectDispatchGroup,
    **facts: object,
) -> None:
    """Emit one content-free receipt that can join a long-chain diagnosis."""

    receipt = {
        "event": "followup_boundary_receipt",
        "followup_stage": stage,
        "parent_run_id": group.parent_run_id,
        "dispatch_group_id": group.dispatch_group_id,
        "followup_group_state": group.state,
        "followup_member_count": group.member_count,
        "followup_wire_digest": group.wire_text_digest,
        **facts,
    }
    # Runtime's production formatter does not render LogRecord extras.  Keep
    # the structured extras for collectors and include the same content-free
    # receipt in the message so one candidate reload yields usable evidence.
    logger.warning(
        "followup_boundary_receipt %s",
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


_HISTORY_DIGEST_FIELDS = (
    "id",
    "session_id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "effect_disposition",
    "timestamp",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
    "platform_message_id",
    "active",
    "compacted",
    "api_content",
)
_HISTORY_JSON_FIELDS = frozenset(
    {
        "tool_calls",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
    }
)
_REDIRECT_CONTEXT_PREFIX = "[Context from the interrupted assistant response]\n"
_REDIRECT_INTERRUPT_MARKER = (
    "[This response was interrupted by a user correction.]"
)
_REDIRECT_VISIBLE_HEADER = "Visible response before the interruption:"
_STEER_OPEN = (
    "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once "
    "at this position; not tool output and not a new delivery when replayed from "
    "conversation history]"
)
_STEER_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
_REDIRECT_DISALLOWED_FIELDS = (
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "effect_disposition",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


@dataclass(frozen=True, slots=True)
class RedirectHistoryAnchor:
    stored_session_id: str
    message_id: int
    prefix_digest: str


@dataclass(frozen=True, slots=True)
class RedirectCanonicalProof:
    message_id: int
    post_digest: str


@dataclass(frozen=True, slots=True)
class SteerModelRequestProofObservation:
    proof: RedirectCanonicalProof | None
    reason: str
    request_tool_count: int = 0
    history_tool_count: int = 0


class RedirectHistorySource(Protocol):
    async def get_session_messages(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]: ...


RedirectDispatchRecorder = Callable[
    [int, str], Awaitable[StoredRedirectDispatchGroup]
]


class RedirectEventFenceController(Protocol):
    async def publish_durable_followup_events(
        self,
        parent_run_id: str,
    ) -> None: ...

    async def begin_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
        recorder: RedirectDispatchRecorder,
    ) -> None: ...

    async def release_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
    ) -> None: ...


class RedirectSessionSettleController(Protocol):
    async def interrupt_and_settle_redirect(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        profile_name: str = "default",
        timeout_seconds: float | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RedirectConfirmationOutcome:
    resolved: bool
    session_settled: bool


class HermesV0201RedirectHistoryVerifier:
    """Strict raw-history proof for the pinned 0.20.1 redirect renderer.

    The accepted wire is the renderer at upstream commit ``f80f453``:
    the correction remains the clean user ``content`` and its provider replay
    form is stored in ``api_content``.  Merely finding the same clean text is
    intentionally insufficient.
    """

    @classmethod
    def anchor(cls, payload: Mapping[str, object]) -> RedirectHistoryAnchor:
        session_id, messages = cls._validated_history(payload)
        high_water = max((cls._message_id(item) for item in messages), default=0)
        return RedirectHistoryAnchor(
            stored_session_id=session_id,
            message_id=high_water,
            prefix_digest=cls._prefix_digest(messages, through_id=high_water),
        )

    @classmethod
    def find_proof(
        cls,
        payload: Mapping[str, object],
        *,
        anchor: RedirectHistoryAnchor,
        wire_text: str,
    ) -> RedirectCanonicalProof | None:
        session_id, messages = cls._validated_history(payload)
        if session_id != anchor.stored_session_id:
            return None
        if cls._prefix_digest(messages, through_id=anchor.message_id) != (
            anchor.prefix_digest
        ):
            return None

        ordered = sorted(messages, key=cls._message_id)
        for index, message in enumerate(ordered):
            message_id = cls._message_id(message)
            if message_id <= anchor.message_id:
                continue
            visible = cls._redirect_visible_text(message, wire_text=wire_text)
            if visible is None:
                continue
            if not cls._valid_placeholder(
                ordered,
                index=index,
                anchor_message_id=anchor.message_id,
                visible=visible,
            ):
                continue
            return RedirectCanonicalProof(
                message_id=message_id,
                post_digest=cls._prefix_digest(messages, through_id=message_id),
            )
        return None

    @classmethod
    def find_steer_proof(
        cls,
        payload: Mapping[str, object],
        *,
        anchor: RedirectHistoryAnchor,
        wire_text: str,
    ) -> RedirectCanonicalProof | None:
        """Prove Hermes persisted its exact 0.20.1 tool-result steer marker."""

        session_id, messages = cls._validated_history(payload)
        if session_id != anchor.stored_session_id:
            return None
        if cls._prefix_digest(messages, through_id=anchor.message_id) != (
            anchor.prefix_digest
        ):
            return None
        marker = f"{_STEER_OPEN}\n{wire_text}\n{_STEER_CLOSE}"
        for message in sorted(messages, key=cls._message_id):
            message_id = cls._message_id(message)
            if message_id <= anchor.message_id or message.get("role") != "tool":
                continue
            content = message.get("content")
            if isinstance(content, str) and marker in content:
                return RedirectCanonicalProof(
                    message_id=message_id,
                    post_digest=cls._prefix_digest(
                        messages,
                        through_id=message_id,
                    ),
                )
        return None

    @classmethod
    def find_steer_model_request_proof(
        cls,
        payload: Mapping[str, object],
        *,
        anchor: RedirectHistoryAnchor,
        wire_text: str,
        request_body: bytes,
    ) -> RedirectCanonicalProof | None:
        return cls.inspect_steer_model_request_proof(
            payload,
            anchor=anchor,
            wire_text=wire_text,
            request_body=request_body,
        ).proof

    @classmethod
    def inspect_steer_model_request_proof(
        cls,
        payload: Mapping[str, object],
        *,
        anchor: RedirectHistoryAnchor,
        wire_text: str,
        request_body: bytes,
    ) -> SteerModelRequestProofObservation:
        """Prove the next main-model request contains Hermes' in-memory steer.

        Hermes 0.20.1 persists the plain tool result before appending the
        pending steer to its in-memory provider message. Therefore state.db
        alone cannot prove delivery. The authenticated Model Relay request
        must contain one persisted tool row plus the exact marker as its suffix.
        """

        try:
            session_id, history_messages = cls._validated_history(payload)
        except ValueError:
            return SteerModelRequestProofObservation(None, "invalid_history")
        if session_id != anchor.stored_session_id:
            return SteerModelRequestProofObservation(
                None,
                "session_lineage_mismatch",
            )
        if cls._prefix_digest(
            history_messages,
            through_id=anchor.message_id,
        ) != anchor.prefix_digest:
            return SteerModelRequestProofObservation(
                None,
                "history_prefix_mismatch",
            )
        try:
            request = json.loads(request_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return SteerModelRequestProofObservation(
                None,
                "invalid_request_json",
            )
        if not isinstance(request, dict):
            return SteerModelRequestProofObservation(
                None,
                "request_not_object",
            )
        raw_request_messages = request.get("messages")
        if not isinstance(raw_request_messages, list):
            return SteerModelRequestProofObservation(
                None,
                "request_messages_missing",
            )
        request_tool_contents = [
            item.get("content")
            for item in raw_request_messages
            if isinstance(item, Mapping)
            and item.get("role") == "tool"
            and isinstance(item.get("content"), str)
        ]
        if not request_tool_contents:
            return SteerModelRequestProofObservation(
                None,
                "request_tool_messages_missing",
            )
        suffix = f"\n\n{_STEER_OPEN}\n{wire_text}\n{_STEER_CLOSE}"
        history_tool_count = 0
        for message in sorted(
            history_messages,
            key=cls._message_id,
            reverse=True,
        ):
            message_id = cls._message_id(message)
            content = message.get("content")
            if (
                message_id <= anchor.message_id
                or message.get("role") != "tool"
                or not isinstance(content, str)
            ):
                continue
            history_tool_count += 1
            expected = content + suffix
            if expected not in request_tool_contents:
                continue
            return SteerModelRequestProofObservation(
                RedirectCanonicalProof(
                    message_id=message_id,
                    post_digest=_sha256_json(
                        {
                            "history_post_digest": cls._prefix_digest(
                                history_messages,
                                through_id=message_id,
                            ),
                            "model_request_tool_digest": _sha256_json(
                                expected
                            ),
                            "proof_kind": "steer_oob_marker",
                        }
                    ),
                ),
                "confirmed",
                request_tool_count=len(request_tool_contents),
                history_tool_count=history_tool_count,
            )
        return SteerModelRequestProofObservation(
            None,
            (
                "history_tool_messages_missing"
                if history_tool_count == 0
                else "exact_tool_suffix_mismatch"
            ),
            request_tool_count=len(request_tool_contents),
            history_tool_count=history_tool_count,
        )

    @classmethod
    def _validated_history(
        cls,
        payload: Mapping[str, object],
    ) -> tuple[str, list[Mapping[str, object]]]:
        session_id = payload.get("session_id")
        raw_messages = payload.get("messages")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("redirect history has no stable Session lineage")
        if not isinstance(raw_messages, list) or not all(
            isinstance(item, Mapping) for item in raw_messages
        ):
            raise ValueError("redirect history messages are invalid")
        messages = list(raw_messages)
        ids = [cls._message_id(item) for item in messages]
        if len(ids) != len(set(ids)):
            raise ValueError("redirect history contains duplicate message ids")
        return session_id.strip(), messages

    @staticmethod
    def _message_id(message: Mapping[str, object]) -> int:
        value = message.get("id")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("redirect history message id is invalid")
        return value

    @classmethod
    def _prefix_digest(
        cls,
        messages: Sequence[Mapping[str, object]],
        *,
        through_id: int,
    ) -> str:
        projection: list[dict[str, object]] = []
        for message in sorted(messages, key=cls._message_id):
            if cls._message_id(message) > through_id:
                continue
            projected: dict[str, object] = {}
            for field in _HISTORY_DIGEST_FIELDS:
                value = message.get(field)
                if field in _HISTORY_JSON_FIELDS and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"redirect history field {field} is not valid JSON"
                        ) from exc
                projected[field] = value
            projection.append(projected)
        return _sha256_json(projection)

    @classmethod
    def _redirect_visible_text(
        cls,
        message: Mapping[str, object],
        *,
        wire_text: str,
    ) -> str | None:
        if (
            message.get("role") != "user"
            or message.get("content") != wire_text
            or any(message.get(field) is not None for field in _REDIRECT_DISALLOWED_FIELDS)
            or message.get("active") in {False, 0}
            or message.get("compacted") in {True, 1}
        ):
            return None
        api_content = message.get("api_content")
        if not isinstance(api_content, str):
            return None
        base = _REDIRECT_CONTEXT_PREFIX + _REDIRECT_INTERRUPT_MARKER
        no_visible = f"{base}\n\n{wire_text}"
        if api_content == no_visible:
            return ""
        prefix = f"{base}\n\n{_REDIRECT_VISIBLE_HEADER}\n\n"
        suffix = f"\n\n{wire_text}"
        if not api_content.startswith(prefix) or not api_content.endswith(suffix):
            return None
        visible = api_content[len(prefix) : -len(suffix)]
        if not visible or visible.strip() != visible:
            return None
        return visible

    @classmethod
    def _valid_placeholder(
        cls,
        messages: Sequence[Mapping[str, object]],
        *,
        index: int,
        anchor_message_id: int,
        visible: str,
    ) -> bool:
        if index == 0:
            return False
        previous = messages[index - 1]
        previous_id = cls._message_id(previous)
        # The upstream "tail already assistant" branch intentionally creates
        # no new placeholder row.
        if previous_id <= anchor_message_id:
            return previous.get("role") == "assistant"
        if (
            previous.get("role") != "assistant"
            or previous.get("content") != visible
            or previous.get("api_content") is not None
            or any(
                previous.get(field) is not None
                for field in _REDIRECT_DISALLOWED_FIELDS
            )
        ):
            return False
        if visible:
            return previous.get("display_kind") in {None, ""}
        return previous.get("display_kind") == "hidden"


@dataclass(slots=True)
class _InflightDispatch:
    task: asyncio.Task[object]
    handle: MainBoundaryHandle


class RedirectBoundaryRouteResolver:
    """Join an authenticated marker to the stream's exact control peer."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        control_peers: ActiveRunControlPeerRegistry,
        boot: MainBoundaryBoot,
    ) -> None:
        self._store = store
        self._control_peers = control_peers
        self._boot = boot

    async def resolve_route(
        self,
        *,
        route: ActiveRouteLike,
        marker: PlatformMainBoundaryMarkerV1,
    ) -> MainBoundaryRouteBinding | None:
        if (
            route.route_token != marker.route_token
            or route.session_id != marker.session_id
        ):
            return None
        parent = await self._store.get_run(route.run_id)
        if parent is None or parent.session_id != route.session_id:
            return None
        connection_id, live_session_id = (
            await self._control_peers.wait_for_route_identity(route.run_id)
        )
        return MainBoundaryRouteBinding(
            route_token=route.route_token,
            run_id=route.run_id,
            dashboard_boot_id=self._boot.dashboard_boot_id,
            dashboard_connection_id=connection_id,
            stored_session_id=route.session_id,
            live_session_id=live_session_id,
            task_id=marker.task_id,
            profile_key=parent.profile_id or "default",
            boundary_epoch_nonce=self._boot.boundary_epoch_nonce,
        )


class RedirectDurableGate:
    """Durable bridge between a verified main boundary and session.redirect."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        control_peers: ActiveRunControlPeerRegistry,
        history_source: RedirectHistorySource,
        event_fences: RedirectEventFenceController,
        settle_controller: RedirectSessionSettleController,
        history_verifier: type[HermesV0201RedirectHistoryVerifier] = (
            HermesV0201RedirectHistoryVerifier
        ),
        rpc_timeout_seconds: float | None = None,
        carrier_proof_seconds: float = REDIRECT_CARRIER_PROOF_SECONDS,
        isolation_timeout_seconds: float = REDIRECT_SESSION_ISOLATION_SECONDS,
    ) -> None:
        self._store = store
        self._control_peers = control_peers
        self._history_source = history_source
        self._event_fences = event_fences
        self._settle_controller = settle_controller
        self._history_verifier = history_verifier
        self._rpc_timeout_seconds = rpc_timeout_seconds
        self._carrier_proof_seconds = max(0.0, carrier_proof_seconds)
        self._isolation_timeout_seconds = max(0.1, isolation_timeout_seconds)
        self._boundary: MainBoundaryCoordinator | None = None
        self._parent_locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, _InflightDispatch] = {}
        self._confirmation_tasks: dict[
            str,
            asyncio.Task[RedirectConfirmationOutcome],
        ] = {}
        self._confirmation_wakes: dict[str, asyncio.Event] = {}
        self._parent_close_tasks: dict[
            str,
            asyncio.Task[RedirectConfirmationOutcome],
        ] = {}
        self._redirect_dispatch_tasks: dict[str, asyncio.Task[None]] = {}
        self._steer_dispatch_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._guard = asyncio.Lock()

    async def start(self) -> None:
        """Resume durable groups without restoring an expired in-memory timer."""

        self._closed = False
        for group in await self._store.list_redirect_groups_with_release_ready_fences():
            await self._event_fences.release_redirect_event_fence(
                parent_run_id=group.parent_run_id,
                dispatch_group_id=group.dispatch_group_id,
            )
        for group in await self._store.list_unresolved_redirect_groups():
            if group.state == "PREPARED":
                await self._arm_if_ready(group.parent_run_id)
                continue
            lock = await self._parent_lock(group.parent_run_id)
            async with lock:
                current = await self._store.get_redirect_group(
                    group.dispatch_group_id
                )
                if current is None or current.state not in {
                    "DISPATCH_INTENT",
                    "RPC_ACKNOWLEDGED",
                    "ISOLATING_UNCERTAIN",
                }:
                    continue
                if await self._reconcile_group(current):
                    await self._wake_confirmation(current.dispatch_group_id)
                    await self._arm_if_ready(current.parent_run_id)
                    continue
                current = await self._isolate_crossed_group(current)
            await self._ensure_confirmation(current, delay_seconds=0.0)

    async def close(self) -> None:
        self._closed = True
        async with self._guard:
            tasks = [
                *self._confirmation_tasks.values(),
                *self._parent_close_tasks.values(),
                *self._redirect_dispatch_tasks.values(),
                *self._steer_dispatch_tasks.values(),
            ]
        for task in tasks:
            task.cancel()
        await asyncio.sleep(0)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def bind_boundary(self, boundary: MainBoundaryCoordinator) -> None:
        if self._boundary is not None and self._boundary is not boundary:
            raise RuntimeError("redirect durable gate already has a boundary")
        self._boundary = boundary

    async def redirect_admitted(self, parent_run_id: str) -> None:
        if await self._control_peers.is_tool_active(parent_run_id):
            await self._schedule_steer_dispatch(parent_run_id)
            return
        await self._schedule_redirect_dispatch(parent_run_id)

    async def _schedule_redirect_dispatch(self, parent_run_id: str) -> None:
        async with self._guard:
            if self._closed or parent_run_id in self._redirect_dispatch_tasks:
                return
            task = asyncio.create_task(
                self._deferred_redirect_dispatch(parent_run_id),
                name=f"followup-redirect-{parent_run_id}",
            )
            self._redirect_dispatch_tasks[parent_run_id] = task

    async def _deferred_redirect_dispatch(self, parent_run_id: str) -> None:
        task = asyncio.current_task()
        try:
            # Freeze rapid A/B/C admissions into one official redirect RPC.
            await asyncio.sleep(0.05)
            if await self._control_peers.is_tool_active(parent_run_id):
                await self._schedule_steer_dispatch(parent_run_id)
                return
            boundary = self._require_boundary()
            handle = await boundary.take_active_upstream_handle(parent_run_id)
            if handle is None:
                await self._arm_if_ready(parent_run_id)
                return
            result = await self.dispatch(handle)
            if result is MainBoundaryDispatchResult.RELEASE:
                await self._arm_if_ready(parent_run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "followup active redirect dispatch failed",
                extra={
                    "event": "followup_active_redirect_dispatch_failed",
                    "parent_run_id": parent_run_id,
                },
            )
        finally:
            async with self._guard:
                if self._redirect_dispatch_tasks.get(parent_run_id) is task:
                    self._redirect_dispatch_tasks.pop(parent_run_id, None)

    async def _schedule_steer_dispatch(self, parent_run_id: str) -> None:
        async with self._guard:
            if self._closed or parent_run_id in self._steer_dispatch_tasks:
                return
            task = asyncio.create_task(
                self._deferred_steer_dispatch(parent_run_id),
                name=f"followup-steer-{parent_run_id}",
            )
            self._steer_dispatch_tasks[parent_run_id] = task

    async def _deferred_steer_dispatch(self, parent_run_id: str) -> None:
        task = asyncio.current_task()
        try:
            # Let the durable pending receipt return first so rapid A/B/C
            # messages can join the same frozen group before the RPC.
            await asyncio.sleep(0.05)
            if not await self._dispatch_steer(parent_run_id):
                await self._arm_if_ready(parent_run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "followup deferred steer dispatch failed",
                extra={
                    "event": "followup_steer_dispatch_failed",
                    "parent_run_id": parent_run_id,
                },
            )
        finally:
            async with self._guard:
                if self._steer_dispatch_tasks.get(parent_run_id) is task:
                    self._steer_dispatch_tasks.pop(parent_run_id, None)

    async def observe_tool_activity(self, *, run_id: str, active: bool) -> None:
        if active and await self._dispatch_steer(run_id):
            return
        if not active:
            await self._arm_if_ready(run_id)

    async def observe_control_peer_registered(
        self,
        *,
        run_id: str,
        stored_session_id: str,
        dashboard_connection_id: str,
    ) -> None:
        await self._require_boundary().observe_control_peer_registered(
            run_id=run_id,
            stored_session_id=stored_session_id,
            dashboard_connection_id=dashboard_connection_id,
        )

    async def observe_message_start(
        self,
        *,
        run_id: str,
        stored_session_id: str,
        live_session_id: str,
        dashboard_connection_id: str,
    ) -> None:
        boundary = self._require_boundary()
        await boundary.observe_message_start(
            run_id=run_id,
            stored_session_id=stored_session_id,
            live_session_id=live_session_id,
            dashboard_connection_id=dashboard_connection_id,
        )
        await self._mark_waiting_boundary_ready(run_id)
        await self._arm_if_ready(run_id)

    async def observe_model_request(
        self,
        *,
        binding: MainBoundaryRouteBinding,
        request_body: bytes,
    ) -> bool:
        """Confirm a queued steer only when the next exact model sees it."""

        lock = await self._parent_lock(binding.run_id)
        async with lock:
            group = await self._store.get_unresolved_redirect_group_for_parent(
                binding.run_id
            )
            if (
                group is None
                or group.state
                not in {
                    "DISPATCH_INTENT",
                    "RPC_ACKNOWLEDGED",
                    "ISOLATING_UNCERTAIN",
                }
                or group.hermes_status != "queued"
                or group.dispatch_peer_boot_id != "direct-steer"
                or group.dispatch_peer_connection_id
                != binding.dashboard_connection_id
                or group.pre_dispatch_message_id is None
                or group.pre_dispatch_history_digest is None
            ):
                return False
            parent = await self._store.get_run(group.parent_run_id)
            if parent is None or parent.session_id != binding.stored_session_id:
                return False
            try:
                history = await self._history_source.get_session_messages(
                    session_id=parent.session_id,
                    profile_name=parent.profile_id or "default",
                )
                observation = (
                    self._history_verifier.inspect_steer_model_request_proof(
                        history,
                        anchor=RedirectHistoryAnchor(
                            stored_session_id=group.hermes_stored_session_id,
                            message_id=group.pre_dispatch_message_id,
                            prefix_digest=group.pre_dispatch_history_digest,
                        ),
                        wire_text=group.wire_text,
                        request_body=request_body,
                    )
                )
            except Exception:
                observation = SteerModelRequestProofObservation(
                    None,
                    "proof_observer_error",
                )
            if observation.proof is None:
                _log_boundary_receipt(
                    "steer_model_request_proof_absent",
                    group,
                    followup_dashboard_connection_id=(
                        binding.dashboard_connection_id
                    ),
                    followup_proof_reason=observation.reason,
                    followup_request_tool_count=(
                        observation.request_tool_count
                    ),
                    followup_history_tool_count=(
                        observation.history_tool_count
                    ),
                )
                return False
            proof = observation.proof
            consumed = await self._store.consume_redirect_group(
                group.dispatch_group_id,
                proof_message_id=proof.message_id,
                proof_post_digest=proof.post_digest,
                consumed_at=datetime.now(timezone.utc),
            )
            _log_boundary_receipt(
                "canonical_proof_confirmed",
                consumed,
                followup_proof_message_id=proof.message_id,
                followup_proof_digest=proof.post_digest,
                followup_proof_source="model_request_steer_marker",
            )
            await self._event_fences.release_redirect_event_fence(
                parent_run_id=group.parent_run_id,
                dispatch_group_id=group.dispatch_group_id,
            )
            await self._wake_confirmation(group.dispatch_group_id)
        await self.redirect_admitted(binding.run_id)
        return True

    async def _dispatch_steer(self, parent_run_id: str) -> bool:
        """Deliver one durable follow-up group at the active tool boundary."""

        await self._mark_waiting_boundary_ready(parent_run_id)
        lock = await self._parent_lock(parent_run_id)
        async with lock:
            if not await self._control_peers.is_tool_active(parent_run_id):
                return False
            group = await self._store.get_unresolved_redirect_group_for_parent(
                parent_run_id
            )
            if group is not None and group.state != "PREPARED":
                return True
            if group is None:
                group = await self._store.prepare_redirect_group(
                    dispatch_group_id=f"redirect_group_{uuid4().hex}",
                    parent_run_id=parent_run_id,
                    created_at=datetime.now(timezone.utc),
                )
            if group is None:
                return False
            parent = await self._store.get_run(parent_run_id)
            if parent is None:
                return False
            try:
                raw_history = await self._history_source.get_session_messages(
                    session_id=parent.session_id,
                    profile_name=parent.profile_id or "default",
                )
                anchor = self._history_verifier.anchor(raw_history)
            except Exception:
                return False
            if not await self._control_peers.is_tool_active(parent_run_id):
                return False
            connection_id = await self._control_peers.current_connection_id(
                parent_run_id
            )
            if connection_id is None:
                return False
            dispatch_nonce = uuid4().hex

            async def record_dispatch(
                pre_fence_event_seq: int,
                pre_fence_token_snapshot_digest: str,
            ) -> StoredRedirectDispatchGroup:
                return await self._store.record_redirect_dispatch_intent(
                    dispatch_group_id=group.dispatch_group_id,
                    hermes_profile_key=parent.profile_id or "default",
                    hermes_stored_session_id=anchor.stored_session_id,
                    pre_dispatch_message_id=anchor.message_id,
                    pre_dispatch_history_digest=anchor.prefix_digest,
                    pre_dispatch_model_request_seq=0,
                    pre_dispatch_model_request_digest=_sha256_json(
                        {"method": "session.steer", "wire": group.wire_text}
                    ),
                    dispatch_turn_id=f"steer-{dispatch_nonce}",
                    dispatch_turn_task_id=f"steer-{dispatch_nonce}",
                    dispatch_origin_evidence_digest=_sha256_json(
                        {
                            "runtime_run_id": parent_run_id,
                            "stored_session_id": anchor.stored_session_id,
                            "dashboard_connection_id": connection_id,
                            "method": "session.steer",
                        }
                    ),
                    dispatch_boundary_epoch_nonce=dispatch_nonce,
                    dispatch_marker_request_nonce=dispatch_nonce,
                    dispatch_peer_boot_id="direct-steer",
                    dispatch_peer_connection_id=connection_id,
                    pre_fence_event_seq=pre_fence_event_seq,
                    pre_fence_token_snapshot_digest=(
                        pre_fence_token_snapshot_digest
                    ),
                    recorded_at=datetime.now(timezone.utc),
                )

            try:
                await self._event_fences.begin_redirect_event_fence(
                    parent_run_id=parent_run_id,
                    dispatch_group_id=group.dispatch_group_id,
                    recorder=record_dispatch,
                )
                anchored = await self._store.get_redirect_group(
                    group.dispatch_group_id
                )
                if anchored is not None:
                    _log_boundary_receipt(
                        "steer_dispatch_anchored",
                        anchored,
                        followup_stored_session_id=anchor.stored_session_id,
                        followup_dashboard_connection_id=connection_id,
                        followup_boundary_epoch_nonce=dispatch_nonce,
                    )
                await self._control_peers.steer(
                    runtime_run_id=parent_run_id,
                    wire_text=group.wire_text,
                    timeout_seconds=self._rpc_timeout_seconds,
                )
                current = await self._store.get_redirect_group(
                    group.dispatch_group_id
                )
                if current is not None:
                    _log_boundary_receipt(
                        "steer_rpc_returned",
                        current,
                        followup_rpc_disposition="queued",
                    )
            except DashboardSteerUncertain:
                isolated = await self._isolate_crossed_group(group)
                await self._ensure_confirmation(isolated, delay_seconds=0.0)
                return True
            except Exception:
                await self._rollback_pre_rpc(group)
                return False

            acknowledged = await self._store.acknowledge_redirect_group(
                group.dispatch_group_id,
                hermes_status="queued",
                queued_is_accepted=True,
                acknowledged_at=datetime.now(timezone.utc),
            )
            if await self._reconcile_group(acknowledged):
                await self._arm_if_ready(parent_run_id)
            else:
                await self._ensure_confirmation(
                    acknowledged,
                    delay_seconds=self._carrier_proof_seconds,
                )
            return True

    async def dispatch(
        self,
        handle: MainBoundaryHandle,
    ) -> MainBoundaryDispatchResult:
        parent_run_id = handle.runtime_run_id
        lock = await self._parent_lock(parent_run_id)
        async with lock:
            task = asyncio.current_task()
            assert task is not None
            async with self._guard:
                if parent_run_id in self._inflight:
                    return MainBoundaryDispatchResult.RELEASE
                self._inflight[parent_run_id] = _InflightDispatch(
                    task=task,
                    handle=handle,
                )
            try:
                return await self._dispatch_locked(handle)
            finally:
                async with self._guard:
                    current = self._inflight.get(parent_run_id)
                    if current is not None and current.task is task:
                        self._inflight.pop(parent_run_id, None)

    async def reconcile_redirect(self, parent_run_id: str) -> bool:
        lock = await self._parent_lock(parent_run_id)
        async with lock:
            group = await self._store.get_unresolved_redirect_group_for_parent(
                parent_run_id
            )
            if group is None or group.state not in {
                "DISPATCH_INTENT",
                "RPC_ACKNOWLEDGED",
                "ISOLATING_UNCERTAIN",
            }:
                return False
            proof = await self._canonical_proof(group)
            if proof is None:
                return False
            await self._store.consume_redirect_group(
                group.dispatch_group_id,
                proof_message_id=proof.message_id,
                proof_post_digest=proof.post_digest,
                consumed_at=datetime.now(timezone.utc),
            )
            await self._event_fences.release_redirect_event_fence(
                parent_run_id=parent_run_id,
                dispatch_group_id=group.dispatch_group_id,
            )
            await self._wake_confirmation(group.dispatch_group_id)
        await self.redirect_admitted(parent_run_id)
        return True

    async def prepare_parent_stop(self, parent_run_id: str) -> bool:
        """Preserve every follow-up before interrupting its parent Run."""

        outcome = await self._ensure_parent_close(
            parent_run_id,
            interrupt_active_session=True,
        )
        return outcome.session_settled

    async def prepare_parent_terminal(self, parent_run_id: str) -> None:
        """Finalize unproven follow-ups after the real parent terminal arrives."""

        await self._ensure_parent_close(
            parent_run_id,
            interrupt_active_session=False,
        )

    async def _ensure_parent_close(
        self,
        parent_run_id: str,
        *,
        interrupt_active_session: bool,
    ) -> RedirectConfirmationOutcome:
        async with self._guard:
            existing = self._parent_close_tasks.get(parent_run_id)
            if existing is None:
                existing = asyncio.create_task(
                    self._close_parent_followups(
                        parent_run_id,
                        interrupt_active_session=interrupt_active_session,
                    ),
                    name=f"followup-parent-close-{parent_run_id}",
                )
                self._parent_close_tasks[parent_run_id] = existing
        try:
            return await asyncio.shield(existing)
        finally:
            if existing.done():
                async with self._guard:
                    if self._parent_close_tasks.get(parent_run_id) is existing:
                        self._parent_close_tasks.pop(parent_run_id, None)

    async def _close_parent_followups(
        self,
        parent_run_id: str,
        *,
        interrupt_active_session: bool,
    ) -> RedirectConfirmationOutcome:
        """Resolve crossed delivery before closing the parent ledger."""

        group = await self._store.get_unresolved_redirect_group_for_parent(
            parent_run_id
        )
        if group is not None and group.state == "DISPATCH_INTENT":
            async with self._guard:
                inflight = self._inflight.get(parent_run_id)
            if inflight is not None and not inflight.handle.claimed:
                if not await self._finalize_parent_pre_rpc(
                    parent_run_id,
                    create_continuation=True,
                ):
                    raise MainBoundaryDispatchUncertain(
                        "redirect changed while canceling unclaimed dispatch"
                    )
                return RedirectConfirmationOutcome(
                    resolved=True,
                    session_settled=False,
                )
        if group is None or group.state == "PREPARED":
            if not await self._finalize_parent_pre_rpc(
                parent_run_id,
                create_continuation=True,
            ):
                raise MainBoundaryDispatchUncertain(
                    "redirect changed while preparing parent Stop"
                )
            return RedirectConfirmationOutcome(resolved=True, session_settled=False)

        lock = await self._parent_lock(parent_run_id)
        async with lock:
            current = await self._store.get_redirect_group(group.dispatch_group_id)
            if current is None:
                return RedirectConfirmationOutcome(
                    resolved=True,
                    session_settled=False,
                )
            if await self._reconcile_group(current):
                current = await self._store.get_redirect_group(
                    group.dispatch_group_id
                )
            if current is not None and current.state == "CANONICAL_CONSUMED":
                session_settled = False
            else:
                current = await self._isolate_crossed_group(current or group)
                session_settled = False

        if current is not None and current.state == "ISOLATING_UNCERTAIN":
            if interrupt_active_session:
                if not current.hermes_stored_session_id:
                    raise MainBoundaryDispatchUncertain(
                        "redirect Session identity is unavailable"
                    )
                await self._settle_controller.interrupt_and_settle_redirect(
                    runtime_run_id=current.parent_run_id,
                    stored_session_id=current.hermes_stored_session_id,
                    profile_name=current.hermes_profile_key or "default",
                    timeout_seconds=self._isolation_timeout_seconds,
                )
                session_settled = True

            async with lock:
                current = await self._store.get_redirect_group(
                    group.dispatch_group_id
                )
                if current is not None and await self._reconcile_group(current):
                    current = await self._store.get_redirect_group(
                        group.dispatch_group_id
                    )
                if current is not None and current.state != "CANONICAL_CONSUMED":
                    if interrupt_active_session:
                        finalized = await self._store.promote_redirect_group(
                            group.dispatch_group_id,
                            promoted_at=datetime.now(timezone.utc),
                            include_all_parent_followups=True,
                        )
                    else:
                        finalized = await self._store.finalize_redirect_group_failed(
                            group.dispatch_group_id,
                            finalized_at=datetime.now(timezone.utc),
                            error_code=(
                                "FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY"
                            ),
                            include_all_parent_followups=True,
                        )
                    _log_boundary_receipt(
                        (
                            "parent_close_failed"
                            if finalized.state == "SUBMISSION_UNCERTAIN"
                            else "parent_close_continued"
                        ),
                        finalized,
                        followup_session_settled=session_settled,
                    )
                    await self._event_fences.release_redirect_event_fence(
                        parent_run_id=finalized.parent_run_id,
                        dispatch_group_id=group.dispatch_group_id,
                    )

        if not await self._finalize_parent_pre_rpc(
            parent_run_id,
            create_continuation=True,
        ):
            raise MainBoundaryDispatchUncertain(
                "redirect changed after parent close settlement"
            )
        boundary = self._boundary
        if boundary is not None:
            await boundary.disarm_redirect(parent_run_id)
        return RedirectConfirmationOutcome(
            resolved=True,
            session_settled=session_settled,
        )

    async def _finalize_parent_pre_rpc(
        self,
        parent_run_id: str,
        *,
        create_continuation: bool,
    ) -> bool:
        """Finalize unclaimed follow-ups without crossing the Hermes RPC."""

        await self._mark_waiting_boundary_ready(parent_run_id)
        async with self._guard:
            inflight = self._inflight.get(parent_run_id)
        if inflight is not None and not inflight.handle.claimed:
            inflight.task.cancel()
            await asyncio.gather(inflight.task, return_exceptions=True)

        lock = await self._parent_lock(parent_run_id)
        async with lock:
            while True:
                group = await self._store.get_unresolved_redirect_group_for_parent(
                    parent_run_id
                )
                if group is not None and group.state == "DISPATCH_INTENT":
                    async with self._guard:
                        current = self._inflight.get(parent_run_id)
                    if current is not None and current.handle.claimed:
                        return False
                    await self._rollback_pre_rpc(group)
                    group = await self._store.get_redirect_group(
                        group.dispatch_group_id
                    )
                if group is not None:
                    if group.state != "PREPARED":
                        return False
                    if create_continuation:
                        finalized = await self._store.promote_redirect_group(
                            group.dispatch_group_id,
                            promoted_at=datetime.now(timezone.utc),
                            include_all_parent_followups=True,
                        )
                    else:
                        finalized = await self._store.finalize_redirect_group_failed(
                            group.dispatch_group_id,
                            finalized_at=datetime.now(timezone.utc),
                            error_code=(
                                "FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY"
                            ),
                            include_all_parent_followups=True,
                        )
                    _log_boundary_receipt(
                        (
                            "parent_terminal_failed"
                            if finalized.state == "SUBMISSION_UNCERTAIN"
                            else "parent_terminal_continued"
                        ),
                        finalized,
                    )
                    break
                group = await self._store.prepare_redirect_group(
                    dispatch_group_id=f"redirect_group_{uuid4().hex}",
                    parent_run_id=parent_run_id,
                    created_at=datetime.now(timezone.utc),
                    allow_canceling_parent=True,
                )
                if group is None:
                    break
                if create_continuation:
                    finalized = await self._store.promote_redirect_group(
                        group.dispatch_group_id,
                        promoted_at=datetime.now(timezone.utc),
                        include_all_parent_followups=True,
                    )
                else:
                    finalized = await self._store.finalize_redirect_group_failed(
                        group.dispatch_group_id,
                        finalized_at=datetime.now(timezone.utc),
                        error_code=(
                            "FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY"
                        ),
                        include_all_parent_followups=True,
                    )
                _log_boundary_receipt(
                    (
                        "parent_terminal_failed"
                        if finalized.state == "SUBMISSION_UNCERTAIN"
                        else "parent_terminal_continued"
                    ),
                    finalized,
                )
                break
        boundary = self._boundary
        if boundary is not None:
            await boundary.disarm_redirect(parent_run_id)
        return True

    async def cancel_parent_pre_rpc(self, parent_run_id: str) -> bool:
        async with self._guard:
            inflight = self._inflight.get(parent_run_id)
        if inflight is not None and not inflight.handle.claimed:
            inflight.task.cancel()
            await asyncio.gather(inflight.task, return_exceptions=True)

        lock = await self._parent_lock(parent_run_id)
        async with lock:
            group = await self._store.get_unresolved_redirect_group_for_parent(
                parent_run_id
            )
            if group is not None and group.state == "DISPATCH_INTENT":
                # A claimed handle is the in-memory proof that the RPC may
                # have crossed.  Without that proof the durable intent is safe
                # to unwind before canceling the parent ledger rows.
                async with self._guard:
                    current = self._inflight.get(parent_run_id)
                if current is not None and current.handle.claimed:
                    return False
                await self._rollback_pre_rpc(group)
            elif group is not None and group.state not in {"PREPARED"}:
                return False
            await self._store.cancel_pre_rpc_redirects_for_parent(
                parent_run_id,
                canceled_at=datetime.now(timezone.utc),
            )
            boundary = self._boundary
            if boundary is not None:
                await boundary.disarm_redirect(parent_run_id)
            return True

    async def _dispatch_locked(
        self,
        handle: MainBoundaryHandle,
    ) -> MainBoundaryDispatchResult:
        parent_run_id = handle.runtime_run_id
        await self._mark_waiting_boundary_ready(parent_run_id)
        group = await self._store.get_unresolved_redirect_group_for_parent(
            parent_run_id
        )
        if group is None:
            group = await self._store.prepare_redirect_group(
                dispatch_group_id=f"redirect_group_{uuid4().hex}",
                parent_run_id=parent_run_id,
                created_at=datetime.now(timezone.utc),
            )
        if group is None:
            return MainBoundaryDispatchResult.RELEASE
        if group.state != "PREPARED":
            if await self._reconcile_group(group):
                return MainBoundaryDispatchResult.REDIRECTED
            raise MainBoundaryDispatchUncertain(
                "redirect group already crossed the RPC boundary"
            )

        parent = await self._store.get_run(parent_run_id)
        if parent is None:
            return MainBoundaryDispatchResult.RELEASE
        try:
            raw_history = await self._history_source.get_session_messages(
                session_id=parent.session_id,
                profile_name=parent.profile_id or "default",
            )
            anchor = self._history_verifier.anchor(raw_history)
        except asyncio.CancelledError:
            _log_boundary_receipt(
                "redirect_anchor_cancelled",
                group,
            )
            raise
        except Exception as exc:
            # History is part of the durable dispatch anchor.  If it cannot be
            # established, the held model request is forwarded untouched.
            _log_boundary_receipt(
                "redirect_anchor_failed",
                group,
                followup_error_type=type(exc).__name__,
            )
            return MainBoundaryDispatchResult.RELEASE

        async def record_dispatch(
            pre_fence_event_seq: int,
            pre_fence_token_snapshot_digest: str,
        ) -> StoredRedirectDispatchGroup:
            return await self._store.record_redirect_dispatch_intent(
                dispatch_group_id=group.dispatch_group_id,
                hermes_profile_key=parent.profile_id or "default",
                hermes_stored_session_id=anchor.stored_session_id,
                pre_dispatch_message_id=anchor.message_id,
                pre_dispatch_history_digest=anchor.prefix_digest,
                pre_dispatch_model_request_seq=handle.api_call_count,
                pre_dispatch_model_request_digest=(
                    f"sha256:{handle.marker_digest}"
                ),
                dispatch_turn_id=handle.turn_id,
                dispatch_turn_task_id=handle.task_id,
                dispatch_origin_evidence_digest=_sha256_json(
                    {
                        "runtime_run_id": handle.runtime_run_id,
                        "stored_session_id": handle.stored_session_id,
                        "live_session_id": handle.live_session_id,
                        "route_token": handle.route_token,
                        "turn_id": handle.turn_id,
                        "task_id": handle.task_id,
                        "api_request_id": handle.api_request_id,
                        "api_call_count": handle.api_call_count,
                    }
                ),
                dispatch_boundary_epoch_nonce=handle.boundary_epoch_nonce,
                dispatch_marker_request_nonce=handle.request_nonce,
                dispatch_peer_boot_id=handle.dashboard_boot_id,
                dispatch_peer_connection_id=handle.dashboard_connection_id,
                pre_fence_event_seq=pre_fence_event_seq,
                pre_fence_token_snapshot_digest=(
                    pre_fence_token_snapshot_digest
                ),
                recorded_at=datetime.now(timezone.utc),
            )

        try:
            await self._event_fences.begin_redirect_event_fence(
                parent_run_id=parent_run_id,
                dispatch_group_id=group.dispatch_group_id,
                recorder=record_dispatch,
            )
            anchored = await self._store.get_redirect_group(
                group.dispatch_group_id
            )
            if anchored is not None:
                _log_boundary_receipt(
                    "redirect_dispatch_anchored",
                    anchored,
                    followup_stored_session_id=handle.stored_session_id,
                    followup_live_session_id=handle.live_session_id,
                    followup_dashboard_boot_id=handle.dashboard_boot_id,
                    followup_dashboard_connection_id=(
                        handle.dashboard_connection_id
                    ),
                    followup_boundary_epoch_nonce=(
                        handle.boundary_epoch_nonce
                    ),
                    followup_request_nonce=handle.request_nonce,
                    followup_model_request_seq=handle.api_call_count,
                    followup_model_request_digest=(
                        f"sha256:{handle.marker_digest}"
                    ),
                )
            result = await self._control_peers.redirect(
                runtime_run_id=parent_run_id,
                wire_text=group.wire_text,
                boundary_handle=handle,
                timeout_seconds=self._rpc_timeout_seconds,
            )
            current = await self._store.get_redirect_group(
                group.dispatch_group_id
            )
            if current is not None:
                _log_boundary_receipt(
                    "redirect_rpc_returned",
                    current,
                    followup_rpc_disposition=result.disposition.value,
                )
        except asyncio.CancelledError:
            if handle.claimed:
                isolated = await self._isolate_crossed_group(group)
                await self._ensure_confirmation(isolated, delay_seconds=0.0)
            else:
                await self._rollback_pre_rpc(group)
            raise
        except (DashboardRedirectUncertain,):
            isolated = await self._isolate_crossed_group(group)
            await self._ensure_confirmation(isolated, delay_seconds=0.0)
            raise MainBoundaryDispatchUncertain(
                "session.redirect disposition is uncertain"
            ) from None
        except (HermesProtocolError, SQLiteStoreError) as exc:
            if handle.claimed:
                isolated = await self._isolate_crossed_group(group)
                await self._ensure_confirmation(isolated, delay_seconds=0.0)
                raise MainBoundaryDispatchUncertain(
                    "session.redirect failed after the RPC boundary"
                ) from None
            _log_boundary_receipt(
                "redirect_pre_rpc_failed",
                group,
                followup_error_type=type(exc).__name__,
            )
            await self._rollback_pre_rpc(group)
            return MainBoundaryDispatchResult.RELEASE
        except Exception as exc:
            if handle.claimed:
                isolated = await self._isolate_crossed_group(group)
                await self._ensure_confirmation(isolated, delay_seconds=0.0)
                raise MainBoundaryDispatchUncertain(
                    "session.redirect failed after the RPC boundary"
                ) from None
            _log_boundary_receipt(
                "redirect_pre_rpc_failed",
                group,
                followup_error_type=type(exc).__name__,
            )
            await self._rollback_pre_rpc(group)
            return MainBoundaryDispatchResult.RELEASE

        if result.disposition is DashboardRedirectDisposition.REDIRECTED:
            acknowledged = await self._store.acknowledge_redirect_group(
                group.dispatch_group_id,
                hermes_status="redirected",
                acknowledged_at=datetime.now(timezone.utc),
            )
            if await self._reconcile_group(acknowledged):
                await self._wake_confirmation(group.dispatch_group_id)
                await self._arm_if_ready(parent_run_id)
            else:
                await self._ensure_confirmation(
                    acknowledged,
                    delay_seconds=self._carrier_proof_seconds,
                )
            return MainBoundaryDispatchResult.REDIRECTED
        if result.disposition is DashboardRedirectDisposition.REJECTED:
            if await self._reconcile_group(group):
                await self._arm_if_ready(parent_run_id)
                return MainBoundaryDispatchResult.REDIRECTED
            promoted = await self._store.promote_redirect_group(
                group.dispatch_group_id,
                promoted_at=datetime.now(timezone.utc),
            )
            _log_boundary_receipt(
                (
                    "redirect_rejected_failed"
                    if promoted.state == "SUBMISSION_UNCERTAIN"
                    else "redirect_rejected_continued"
                ),
                promoted,
                followup_rpc_disposition=result.disposition.value,
            )
            await self._event_fences.release_redirect_event_fence(
                parent_run_id=parent_run_id,
                dispatch_group_id=group.dispatch_group_id,
            )
            return MainBoundaryDispatchResult.REJECTED

        isolated = await self._store.acknowledge_redirect_group(
            group.dispatch_group_id,
            hermes_status="queued",
            acknowledged_at=datetime.now(timezone.utc),
        )
        if await self._reconcile_group(isolated):
            await self._wake_confirmation(group.dispatch_group_id)
            await self._arm_if_ready(parent_run_id)
            return MainBoundaryDispatchResult.REDIRECTED
        await self._ensure_confirmation(isolated, delay_seconds=0.0)
        return MainBoundaryDispatchResult.QUEUED

    async def _reconcile_group(self, group: StoredRedirectDispatchGroup) -> bool:
        current = await self._store.get_redirect_group(group.dispatch_group_id)
        if current is not None:
            group = current
        proof = await self._canonical_proof(group)
        if proof is None:
            _log_boundary_receipt("canonical_proof_absent", group)
            return False
        consumed = await self._store.consume_redirect_group(
            group.dispatch_group_id,
            proof_message_id=proof.message_id,
            proof_post_digest=proof.post_digest,
            consumed_at=datetime.now(timezone.utc),
        )
        _log_boundary_receipt(
            "canonical_proof_confirmed",
            consumed,
            followup_proof_message_id=proof.message_id,
            followup_proof_digest=proof.post_digest,
        )
        await self._event_fences.release_redirect_event_fence(
            parent_run_id=group.parent_run_id,
            dispatch_group_id=group.dispatch_group_id,
        )
        return True

    async def _canonical_proof(
        self,
        group: StoredRedirectDispatchGroup,
    ) -> RedirectCanonicalProof | None:
        if (
            group.pre_dispatch_message_id is None
            or group.pre_dispatch_history_digest is None
            or group.hermes_stored_session_id is None
        ):
            return None
        parent = await self._store.get_run(group.parent_run_id)
        if parent is None:
            return None
        try:
            history = await self._history_source.get_session_messages(
                session_id=parent.session_id,
                profile_name=parent.profile_id or "default",
            )
            anchor = RedirectHistoryAnchor(
                stored_session_id=group.hermes_stored_session_id,
                message_id=group.pre_dispatch_message_id,
                prefix_digest=group.pre_dispatch_history_digest,
            )
            if group.dispatch_peer_boot_id == "direct-steer":
                return self._history_verifier.find_steer_proof(
                    history,
                    anchor=anchor,
                    wire_text=group.wire_text,
                )
            return self._history_verifier.find_proof(
                history,
                anchor=anchor,
                wire_text=group.wire_text,
            )
        except Exception:
            return None

    async def _rollback_pre_rpc(
        self,
        group: StoredRedirectDispatchGroup,
    ) -> None:
        current = await self._store.get_redirect_group(group.dispatch_group_id)
        if current is not None and current.state == "DISPATCH_INTENT":
            await self._store.rollback_pre_rpc_redirect_group(
                group.dispatch_group_id,
                rolled_back_at=datetime.now(timezone.utc),
            )
        await self._event_fences.release_redirect_event_fence(
            parent_run_id=group.parent_run_id,
            dispatch_group_id=group.dispatch_group_id,
        )

    async def _isolate_crossed_group(
        self,
        group: StoredRedirectDispatchGroup,
    ) -> StoredRedirectDispatchGroup:
        try:
            return await self._store.isolate_redirect_group(
                group.dispatch_group_id,
                isolated_at=datetime.now(timezone.utc),
            )
        except SQLiteStoreError:
            # Preserve the original uncertain boundary; a startup reconcile
            # will still see the durable DISPATCH_INTENT.
            current = await self._store.get_redirect_group(
                group.dispatch_group_id
            )
            return current or group

    async def _ensure_confirmation(
        self,
        group: StoredRedirectDispatchGroup,
        *,
        delay_seconds: float,
    ) -> asyncio.Task[RedirectConfirmationOutcome]:
        async with self._guard:
            existing = self._confirmation_tasks.get(group.dispatch_group_id)
            if existing is not None:
                return existing
            if self._closed:
                raise RuntimeError("redirect durable gate is closed")
            wake = asyncio.Event()
            self._confirmation_wakes[group.dispatch_group_id] = wake
            task = asyncio.create_task(
                self._confirm_group(
                    group.dispatch_group_id,
                    delay_seconds=max(0.0, delay_seconds),
                    wake=wake,
                ),
                name=f"redirect-confirmation-{group.dispatch_group_id}",
            )
            task.add_done_callback(self._observe_confirmation_result)
            task.add_done_callback(
                lambda completed, group_id=group.dispatch_group_id: (
                    asyncio.create_task(
                        self._forget_confirmation(group_id, completed)
                    )
                )
            )
            self._confirmation_tasks[group.dispatch_group_id] = task
            return task

    async def _forget_confirmation(
        self,
        dispatch_group_id: str,
        task: asyncio.Task[RedirectConfirmationOutcome],
    ) -> None:
        async with self._guard:
            if self._confirmation_tasks.get(dispatch_group_id) is task:
                self._confirmation_tasks.pop(dispatch_group_id, None)
                self._confirmation_wakes.pop(dispatch_group_id, None)

    async def _wake_confirmation(self, dispatch_group_id: str) -> None:
        async with self._guard:
            wake = self._confirmation_wakes.get(dispatch_group_id)
        if wake is not None:
            wake.set()

    @staticmethod
    def _observe_confirmation_result(
        task: asyncio.Task[RedirectConfirmationOutcome],
    ) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("redirect durable confirmation task failed")

    async def _confirm_group(
        self,
        dispatch_group_id: str,
        *,
        delay_seconds: float,
        wake: asyncio.Event,
    ) -> RedirectConfirmationOutcome:
        if delay_seconds > 0:
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay_seconds)
            except TimeoutError:
                pass

        group = await self._store.get_redirect_group(dispatch_group_id)
        if group is None:
            return RedirectConfirmationOutcome(resolved=True, session_settled=False)
        lock = await self._parent_lock(group.parent_run_id)
        async with lock:
            current = await self._store.get_redirect_group(dispatch_group_id)
            if current is None or current.state in {
                "CANONICAL_CONSUMED",
                "SUBMISSION_UNCERTAIN",
                "PROMOTED_TO_RUN",
                "CANCELED_WITH_PARENT",
            }:
                return RedirectConfirmationOutcome(
                    resolved=True,
                    session_settled=False,
                )
            if await self._reconcile_group(current):
                await self._arm_if_ready(current.parent_run_id)
                return RedirectConfirmationOutcome(
                    resolved=True,
                    session_settled=False,
                )
            return RedirectConfirmationOutcome(
                resolved=False,
                session_settled=False,
            )

    async def _mark_waiting_boundary_ready(self, parent_run_id: str) -> int:
        parent = await self._store.get_run(parent_run_id)
        if parent is None:
            return 0
        changed = 0
        for reservation in await self._store.list_input_admission_reservations(
            parent.session_id
        ):
            if reservation.kind != "redirect" or reservation.state == "promoted":
                break
            redirect = await self._store.get_redirect_by_message(
                session_id=parent.session_id,
                client_message_id=reservation.client_message_id,
            )
            if redirect is None or redirect.parent_run_id != parent_run_id:
                break
            if redirect.state == "WAITING_AGENT_READY":
                await self._store.mark_redirect_boundary_ready(
                    redirect.redirect_id,
                    updated_at=datetime.now(timezone.utc),
                )
                changed += 1
        if changed:
            await self._event_fences.publish_durable_followup_events(
                parent_run_id
            )
        return changed

    async def _arm_if_ready(self, parent_run_id: str) -> None:
        boundary = self._boundary
        if boundary is None:
            return
        if not await self._control_peers.is_agent_ready(parent_run_id):
            return
        if await self._control_peers.is_tool_active(parent_run_id):
            return
        await self._mark_waiting_boundary_ready(parent_run_id)
        group = await self._store.get_unresolved_redirect_group_for_parent(
            parent_run_id
        )
        if group is not None and group.state != "PREPARED":
            return
        parent = await self._store.get_run(parent_run_id)
        if parent is None:
            return
        for reservation in await self._store.list_input_admission_reservations(
            parent.session_id
        ):
            if reservation.kind != "redirect" or reservation.state == "promoted":
                return
            redirect = await self._store.get_redirect_by_message(
                session_id=parent.session_id,
                client_message_id=reservation.client_message_id,
            )
            if (
                redirect is not None
                and redirect.parent_run_id == parent_run_id
                and redirect.state == "WAITING_REDIRECT_BOUNDARY"
            ):
                await boundary.arm_redirect(parent_run_id)
                return

    async def _parent_lock(self, parent_run_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._parent_locks.setdefault(parent_run_id, asyncio.Lock())

    def _require_boundary(self) -> MainBoundaryCoordinator:
        if self._boundary is None:
            raise RuntimeError("redirect durable gate has no boundary coordinator")
        return self._boundary


__all__ = [
    "HermesV0201RedirectHistoryVerifier",
    "RedirectCanonicalProof",
    "RedirectBoundaryRouteResolver",
    "RedirectDurableGate",
    "RedirectEventFenceController",
    "RedirectHistoryAnchor",
    "RedirectHistorySource",
]
