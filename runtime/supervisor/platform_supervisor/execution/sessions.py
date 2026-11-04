from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import time

from platform_supervisor.storage import SQLiteStore


logger = logging.getLogger(__name__)


class SessionCapacityBusy(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PrepareIdentity:
    runtime_id: str
    session_id: str
    profile_id: str
    model_id: str
    config_revision: str
    admission_generation: int
    upstream_model_id: str | None = None
    context_length: int = 128_000
    runtime_capabilities: dict[str, str] | None = None
    max_output_tokens: int = 32_768
    api_mode: str = "chat_completions"
    user_id: int = 0
    org_id: int = 0
    runtime_agent_profile_id: int = 0
    application_agent_id: int | None = None
    profile_desired_revision: int = 0
    profile_applied_revision: int = 0
    profile_digest: str = "none"
    skill_digest: str = "none"
    mcp_digest: str = "none"
    knowledge_digest: str = "none"
    provider_route: dict[str, object] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    provider_route_digest: str | None = None

    def __post_init__(self) -> None:
        if self.admission_generation <= 0:
            raise ValueError("admission_generation must be positive")
        if self.context_length <= 0:
            raise ValueError("context_length must be positive")
        if (self.provider_route is None) != (self.provider_route_digest is None):
            raise ValueError("provider route and digest must be supplied together")
        if self.provider_route_digest is not None:
            if not self.provider_route_digest.startswith("sha256:") or len(
                self.provider_route_digest
            ) != 71:
                raise ValueError("provider route digest is invalid")
            if self.provider_route.get("digest") != self.provider_route_digest:
                raise ValueError("provider route digest does not match grant")

    @property
    def gateway_model_id(self) -> str:
        return self.upstream_model_id or self.model_id

    @property
    def digest(self) -> str:
        payload = {
            "config_revision": self.config_revision,
            "admission_generation": self.admission_generation,
            "knowledge_digest": self.knowledge_digest,
            "mcp_digest": self.mcp_digest,
            "model_id": self.model_id,
            "upstream_model_id": self.gateway_model_id,
            "context_length": self.context_length,
            "runtime_capabilities": self.runtime_capabilities or {},
            "max_output_tokens": self.max_output_tokens,
            "api_mode": self.api_mode,
            "profile_id": self.profile_id,
            "runtime_agent_profile_id": self.runtime_agent_profile_id,
            "application_agent_id": self.application_agent_id,
            "profile_desired_revision": self.profile_desired_revision,
            "profile_applied_revision": self.profile_applied_revision,
            "profile_digest": self.profile_digest,
            "org_id": self.org_id,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "skill_digest": self.skill_digest,
            "user_id": self.user_id,
            "provider_route_digest": self.provider_route_digest,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(slots=True)
class PreparedSession:
    identity: PrepareIdentity
    prepared_at: float
    last_activity_at: float
    agent_generation: int = 0
    active_runs: int = 0
    selected_clients: int = 0

    @property
    def idle(self) -> bool:
        return self.active_runs == 0


PrepareCallback = Callable[[PrepareIdentity], Awaitable[None]]
CloseCallback = Callable[[PrepareIdentity], Awaitable[None]]
ProfileRefreshCallback = Callable[[PrepareIdentity], Awaitable[None]]


class SessionManager:
    """Singleflight Prepare plus the confirmed 500+64/600s/900s policy."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        runtime_id: str,
        max_inflight_runs: int,
        post_run_warm_grace_seconds: int,
        hot_session_limit: int,
        hot_session_ttl_seconds: int,
        prepare_callback: PrepareCallback,
        close_callback: CloseCallback,
        profile_refresh_callback: ProfileRefreshCallback | None = None,
        max_resident_sessions: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.runtime_id = runtime_id
        derived_resident_limit = max_inflight_runs + hot_session_limit
        self.max_resident_sessions = int(
            max_resident_sessions
            if max_resident_sessions is not None
            else derived_resident_limit
        )
        if self.max_resident_sessions < max_inflight_runs:
            raise ValueError(
                "resident Session limit cannot be lower than Run capacity"
            )
        self.post_run_warm_grace_seconds = post_run_warm_grace_seconds
        self.hot_session_limit = hot_session_limit
        self.hot_session_ttl_seconds = hot_session_ttl_seconds
        self.prepare_callback = prepare_callback
        self.close_callback = close_callback
        self.profile_refresh_callback = profile_refresh_callback
        self.clock = clock
        self._sessions: dict[str, PreparedSession] = {}
        self._preparing: dict[str, asyncio.Task[PreparedSession]] = {}
        self._invalidating: dict[str, asyncio.Task[None]] = {}
        self._pending_invalidations: set[str] = set()
        self._guard = asyncio.Lock()
        self._foreground_prepares = 0
        self._restore_task: asyncio.Task[None] | None = None
        self._agent_generation = 0

    @property
    def resident_count(self) -> int:
        return len(self._sessions)

    async def metrics_snapshot(self) -> dict[str, int]:
        async with self._guard:
            sessions = tuple(self._sessions.values())
            return {
                "resident": len(sessions),
                "resident_limit": self.max_resident_sessions,
                "active": sum(
                    1 for session in sessions if session.active_runs > 0
                ),
                "warm_idle": sum(
                    1 for session in sessions if session.active_runs == 0
                ),
                "selected": sum(
                    1 for session in sessions if session.selected_clients > 0
                ),
                "preparing": len(self._preparing),
                "invalidating": len(self._invalidating),
                "foreground_prepares": self._foreground_prepares,
                "agent_generation": self._agent_generation,
                "stale_agents": sum(
                    1
                    for session in sessions
                    if session.agent_generation != self._agent_generation
                ),
            }

    async def prepare(self, identity: PrepareIdentity) -> PreparedSession:
        async with self._guard:
            self._foreground_prepares += 1
        try:
            return await self._prepare(identity)
        finally:
            async with self._guard:
                self._foreground_prepares = max(
                    0,
                    self._foreground_prepares - 1,
                )

    async def _prepare(self, identity: PrepareIdentity) -> PreparedSession:
        if identity.runtime_id != self.runtime_id:
            raise ValueError("Prepare identity belongs to another Runtime")
        stale: PreparedSession | None = None
        wait_for_existing = False
        task: asyncio.Task[PreparedSession] | None = None
        while task is None:
            invalidating: asyncio.Task[None] | None = None
            async with self._guard:
                invalidating = self._invalidating.get(identity.session_id)
                if invalidating is None:
                    agent_generation = self._agent_generation
                    current = self._sessions.get(identity.session_id)
                    if (
                        current is not None
                        and current.identity == identity
                        and current.agent_generation == agent_generation
                    ):
                        logger.debug("runtime_session_prepare_cache_hit")
                        current.last_activity_at = self.clock()
                        return current
                    if current is not None:
                        if current.active_runs:
                            raise SessionCapacityBusy(
                                "Session configuration changed while a Run is active"
                            )
                        stale = self._sessions.pop(identity.session_id)
                    task = self._preparing.get(identity.session_id)
                    if task is None:
                        logger.debug("runtime_session_prepare_cache_miss")
                        task = asyncio.create_task(
                            self._close_then_prepare(
                                stale,
                                identity,
                                agent_generation,
                            ),
                            name=f"prepare-{identity.session_id}",
                        )
                        self._preparing[identity.session_id] = task
                    else:
                        wait_for_existing = True
            if invalidating is not None:
                await asyncio.shield(invalidating)
                await self._forget_invalidation(
                    identity.session_id,
                    invalidating,
                )
        assert task is not None
        try:
            prepared = await asyncio.shield(task)
            async with self._guard:
                current_generation = self._agent_generation
            if (
                (wait_for_existing and prepared.identity != identity)
                or prepared.agent_generation != current_generation
            ):
                async with self._guard:
                    if self._preparing.get(identity.session_id) is task:
                        self._preparing.pop(identity.session_id, None)
                return await self._prepare(identity)
            return prepared
        finally:
            if task.done():
                async with self._guard:
                    if self._preparing.get(identity.session_id) is task:
                        self._preparing.pop(identity.session_id, None)

    async def _close_then_prepare(
        self,
        stale: PreparedSession | None,
        identity: PrepareIdentity,
        agent_generation: int,
    ) -> PreparedSession:
        if stale is not None:
            await self.close_callback(stale.identity)
        return await self._prepare_once(identity, agent_generation)

    async def mark_run_started(self, identity: PrepareIdentity) -> None:
        async with self._guard:
            session = self._sessions.get(identity.session_id)
            if (
                session is None
                or session.identity != identity
                or session.agent_generation != self._agent_generation
            ):
                raise SessionCapacityBusy(
                    "Session Agent changed before this Run started"
                )
            session.active_runs += 1
            session.last_activity_at = self.clock()

    async def mark_run_terminal(self, session_id: str) -> None:
        identity: PrepareIdentity | None = None
        invalidating: asyncio.Task[None] | None = None
        async with self._guard:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if session.active_runs <= 0:
                raise RuntimeError("Session terminal release has no active Run")
            session.active_runs -= 1
            session.last_activity_at = self.clock()
            identity = session.identity
            if (
                session.active_runs == 0
                and (
                    session.agent_generation != self._agent_generation
                    or session_id in self._pending_invalidations
                )
            ):
                self._pending_invalidations.discard(session_id)
                stale = self._sessions.pop(session_id)
                invalidating = self._schedule_invalidation_locked(stale)
        if invalidating is not None:
            await invalidating
            await self._forget_invalidation(session_id, invalidating)
            return
        if identity is not None:
            await self.store.upsert_session_envelope(
                session_id=identity.session_id,
                prepare_identity_digest=identity.digest,
                state="READY",
                last_activity_at=datetime.now(timezone.utc),
                prepare_identity=self._identity_payload(identity),
            )

    def start_background_restore(self, *, config_revision: str) -> None:
        if self.hot_session_limit <= 0 or self._restore_task is not None:
            return
        self._restore_task = asyncio.create_task(
            self._restore_recent(config_revision),
            name="runtime-recent-session-restore",
        )

    async def select(self, session_id: str) -> None:
        async with self._guard:
            session = self._sessions.get(session_id)
            if session is not None:
                session.selected_clients += 1
                session.last_activity_at = self.clock()

    async def deselect(self, session_id: str) -> None:
        async with self._guard:
            session = self._sessions.get(session_id)
            if session is not None:
                session.selected_clients = max(0, session.selected_clients - 1)

    async def reap(self) -> int:
        now = self.clock()
        victims: list[PreparedSession] = []
        async with self._guard:
            idle = [session for session in self._sessions.values() if session.idle]
            for session in idle:
                idle_for = now - session.last_activity_at
                if (
                    idle_for >= self.hot_session_ttl_seconds
                    and session.selected_clients == 0
                ):
                    victims.append(session)
            remaining_idle = [
                session
                for session in idle
                if session not in victims
                and now - session.last_activity_at
                >= self.post_run_warm_grace_seconds
                and session.selected_clients == 0
            ]
            excess_hot = max(
                0,
                len(idle) - len(victims) - self.hot_session_limit,
            )
            victims.extend(
                sorted(
                    remaining_idle,
                    key=lambda item: item.last_activity_at,
                )[:excess_hot]
            )
            resident_excess = max(
                0,
                len(self._sessions) - len(set(id(item) for item in victims))
                - self.max_resident_sessions,
            )
            if resident_excess:
                other_idle = [
                    session
                    for session in idle
                    if session not in victims and session.selected_clients == 0
                ]
                victims.extend(
                    sorted(
                        other_idle,
                        key=lambda item: item.last_activity_at,
                    )[:resident_excess]
                )
            unique = {item.identity.session_id: item for item in victims}
            for session_id, victim in unique.items():
                if self._sessions.get(session_id) is victim:
                    self._sessions.pop(session_id)
        await asyncio.gather(
            *(self.close_callback(item.identity) for item in unique.values()),
            return_exceptions=True,
        )
        return len(unique)

    async def invalidate_idle_for_config(self, config_revision: str) -> int:
        del config_revision
        victims: list[PreparedSession] = []
        async with self._guard:
            for session_id, session in tuple(self._sessions.items()):
                if session.idle:
                    victims.append(session)
                    self._sessions.pop(session_id, None)
        await asyncio.gather(
            *(self.close_callback(item.identity) for item in victims),
            return_exceptions=True,
        )
        return len(victims)

    async def advance_agent_generation(self, reason: str) -> int:
        """Expire every resident or preparing Agent after a global reload.

        Idle Agents are released immediately. Active Agents retain their
        current Run and are released by ``mark_run_terminal``. A prepare that
        crossed this boundary carries the old generation and retries before
        it can be returned to Run admission.
        """

        invalidations: list[tuple[str, asyncio.Task[None]]] = []
        async with self._guard:
            self._agent_generation += 1
            generation = self._agent_generation
            marked_session_ids = (
                set(self._sessions)
                | set(self._preparing)
                | set(self._invalidating)
            )
            active_count = 0
            for session_id, session in tuple(self._sessions.items()):
                if session.idle:
                    self._sessions.pop(session_id, None)
                    invalidation = self._schedule_invalidation_locked(session)
                    invalidations.append((session_id, invalidation))
                else:
                    active_count += 1
            preparing_count = len(self._preparing)
            for session_id, task in self._invalidating.items():
                if all(existing is not task for _, existing in invalidations):
                    invalidations.append((session_id, task))
        await asyncio.gather(*(task for _, task in invalidations))
        for session_id, task in invalidations:
            await self._forget_invalidation(session_id, task)
        logger.warning(
            "runtime_agent_generation_advanced "
            "generation=%s reason=%s marked=%s idle=%s active=%s preparing=%s",
            generation,
            reason,
            len(marked_session_ids),
            len(invalidations),
            active_count,
            preparing_count,
        )
        return len(marked_session_ids)

    async def invalidate(self, session_id: str) -> bool:
        victim: PreparedSession | None = None
        async with self._guard:
            current = self._sessions.get(session_id)
            if current is not None and current.active_runs:
                self._pending_invalidations.add(session_id)
                raise SessionCapacityBusy(
                    "Session cannot be changed while a Run is active"
                )
            if current is not None:
                victim = self._sessions.pop(session_id)
        if victim is None:
            return False
        await self.close_callback(victim.identity)
        await self.store.upsert_session_envelope(
            session_id=victim.identity.session_id,
            prepare_identity_digest=victim.identity.digest,
            state="INVALIDATED",
            last_activity_at=datetime.now(timezone.utc),
            prepare_identity=self._identity_payload(victim.identity),
        )
        return True

    async def close(self) -> None:
        if self._restore_task is not None:
            self._restore_task.cancel()
            await asyncio.gather(
                self._restore_task,
                return_exceptions=True,
            )
            self._restore_task = None
        async with self._guard:
            sessions = list(self._sessions.values())
            preparing = list(self._preparing.values())
            invalidating = list(self._invalidating.values())
            self._sessions.clear()
            self._preparing.clear()
            self._invalidating.clear()
            self._pending_invalidations.clear()
        for task in preparing:
            task.cancel()
        await asyncio.gather(*preparing, return_exceptions=True)
        await asyncio.gather(*invalidating, return_exceptions=True)
        await asyncio.gather(
            *(self.close_callback(item.identity) for item in sessions),
            return_exceptions=True,
        )

    async def _prepare_once(
        self,
        identity: PrepareIdentity,
        agent_generation: int,
    ) -> PreparedSession:
        await self._evict_for_new_resident()
        now = self.clock()
        previous = await self.store.get_session_envelope(identity.session_id)
        if (
            self.profile_refresh_callback is not None
            and self._profile_revision_changed(previous, identity)
        ):
            await self.profile_refresh_callback(identity)
        await self.store.upsert_session_envelope(
            session_id=identity.session_id,
            prepare_identity_digest=identity.digest,
            state="PREPARING",
            last_activity_at=datetime.now(timezone.utc),
            prepare_identity=self._identity_payload(identity),
        )
        try:
            await self.prepare_callback(identity)
        except BaseException:
            await self.store.upsert_session_envelope(
                session_id=identity.session_id,
                prepare_identity_digest=identity.digest,
                state="INVALIDATED",
                last_activity_at=datetime.now(timezone.utc),
                prepare_identity=self._identity_payload(identity),
            )
            raise
        prepared = PreparedSession(
            identity=identity,
            prepared_at=now,
            last_activity_at=now,
            agent_generation=agent_generation,
        )
        async with self._guard:
            self._sessions[identity.session_id] = prepared
        await self.store.upsert_session_envelope(
            session_id=identity.session_id,
            prepare_identity_digest=identity.digest,
            state="READY",
            last_activity_at=datetime.now(timezone.utc),
            prepare_identity=self._identity_payload(identity),
        )
        return prepared

    @staticmethod
    def _profile_revision_changed(
        previous,
        identity: PrepareIdentity,
    ) -> bool:
        if previous is None or previous.prepare_identity is None:
            return False
        try:
            old = PrepareIdentity(**previous.prepare_identity)
        except (TypeError, ValueError):
            return False
        if (
            old.session_id != identity.session_id
            or old.profile_id != identity.profile_id
            or old.runtime_agent_profile_id != identity.runtime_agent_profile_id
            or old.application_agent_id != identity.application_agent_id
        ):
            return False
        return any(
            (
                (
                    identity.runtime_agent_profile_id == 0
                    and old.config_revision != identity.config_revision
                ),
                old.profile_applied_revision != identity.profile_applied_revision,
                old.profile_digest != identity.profile_digest,
                old.skill_digest != identity.skill_digest,
                old.mcp_digest != identity.mcp_digest,
                old.knowledge_digest != identity.knowledge_digest,
            )
        )

    async def _close_invalidated(self, session: PreparedSession) -> None:
        try:
            await self.close_callback(session.identity)
        except Exception:
            logger.exception(
                "runtime_stale_agent_close_failed",
                extra={"session_id": session.identity.session_id},
            )
        try:
            await self.store.upsert_session_envelope(
                session_id=session.identity.session_id,
                prepare_identity_digest=session.identity.digest,
                state="INVALIDATED",
                last_activity_at=datetime.now(timezone.utc),
                prepare_identity=self._identity_payload(session.identity),
            )
        except Exception:
            logger.exception(
                "runtime_stale_agent_envelope_invalidation_failed",
                extra={"session_id": session.identity.session_id},
            )

    def _schedule_invalidation_locked(
        self,
        session: PreparedSession,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._close_invalidated(session),
            name=f"invalidate-agent-{session.identity.session_id}",
        )
        self._invalidating[session.identity.session_id] = task
        return task

    async def _forget_invalidation(
        self,
        session_id: str,
        task: asyncio.Task[None],
    ) -> None:
        async with self._guard:
            if self._invalidating.get(session_id) is task:
                self._invalidating.pop(session_id, None)

    async def _restore_recent(self, config_revision: str) -> None:
        recent = await self.store.load_recent_sessions(
            active_since=datetime.now(timezone.utc)
            - timedelta(seconds=self.hot_session_ttl_seconds),
            limit=self.hot_session_limit,
        )
        for envelope in recent:
            if envelope.state != "READY" or envelope.prepare_identity is None:
                continue
            try:
                identity = PrepareIdentity(**envelope.prepare_identity)
            except (TypeError, ValueError):
                continue
            if (
                identity.runtime_id != self.runtime_id
                or identity.config_revision != config_revision
                or identity.digest != envelope.prepare_identity_digest
            ):
                continue
            while True:
                async with self._guard:
                    foreground_active = self._foreground_prepares > 0
                if not foreground_active:
                    break
                await asyncio.sleep(0.01)
            try:
                await self._prepare(identity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(
                    "Recent Runtime Session restore skipped",
                    extra={
                        "component": "session_manager",
                        "session_id": identity.session_id,
                        "error_type": type(exc).__name__,
                    },
                )
            await asyncio.sleep(0)

    @staticmethod
    def _identity_payload(identity: PrepareIdentity) -> dict[str, object]:
        return {
            "runtime_id": identity.runtime_id,
            "session_id": identity.session_id,
            "profile_id": identity.profile_id,
            "model_id": identity.model_id,
            "config_revision": identity.config_revision,
            "admission_generation": identity.admission_generation,
            "upstream_model_id": identity.upstream_model_id,
            "context_length": identity.context_length,
            "runtime_capabilities": identity.runtime_capabilities or {},
            "max_output_tokens": identity.max_output_tokens,
            "api_mode": identity.api_mode,
            "user_id": identity.user_id,
            "org_id": identity.org_id,
            "runtime_agent_profile_id": identity.runtime_agent_profile_id,
            "application_agent_id": identity.application_agent_id,
            "profile_desired_revision": identity.profile_desired_revision,
            "profile_applied_revision": identity.profile_applied_revision,
            "profile_digest": identity.profile_digest,
            "skill_digest": identity.skill_digest,
            "mcp_digest": identity.mcp_digest,
            "knowledge_digest": identity.knowledge_digest,
            "provider_route": identity.provider_route,
            "provider_route_digest": identity.provider_route_digest,
        }

    async def _evict_for_new_resident(self) -> None:
        victim: PreparedSession | None = None
        async with self._guard:
            # The current Prepare is already present in ``_preparing``. Count
            # all in-flight prepares as resident reservations so a burst cannot
            # overshoot the 500+64 hard limit before callbacks finish.
            if (
                len(self._sessions) + len(self._preparing)
                <= self.max_resident_sessions
            ):
                return
            candidates = [
                session
                for session in self._sessions.values()
                if session.idle and session.selected_clients == 0
            ]
            if candidates:
                victim = min(candidates, key=lambda item: item.last_activity_at)
                self._sessions.pop(victim.identity.session_id, None)
        if victim is None:
            raise SessionCapacityBusy("RUNTIME_SESSION_CAPACITY_BUSY")
        await self.close_callback(victim.identity)
