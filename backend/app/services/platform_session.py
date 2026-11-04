from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Protocol

from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

from app.config import settings
from app.services.runtime_workspace import Principal


PLATFORM_SESSION_COOKIE_NAME = "hermes_platform_session"
ENTERPRISE_IDP_REPLAY_KEY_PREFIX = "hermes:enterprise_idp-replay:v1:"
DATA_APP_LAUNCH_KEY_PREFIX = "hermes:data-app-launch:v1:"

_CREATE_ENTERPRISE_IDP_SESSION_SCRIPT = """
if redis.call("EXISTS", KEYS[2]) == 1 then
  return 0
end
redis.call("SET", KEYS[1], ARGV[1], "EX", ARGV[2])
redis.call("SET", KEYS[2], "used", "EX", ARGV[3])
return 1
"""


class PlatformSessionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PlatformSession:
    session_id: str
    org_id: int
    user_id: int
    created_at: int

    @property
    def principal(self) -> Principal:
        return Principal(org_id=self.org_id, user_id=self.user_id)


@dataclass(frozen=True)
class DataAppLaunch:
    app_id: int
    org_id: int
    user_id: int

    @property
    def principal(self) -> Principal:
        return Principal(org_id=self.org_id, user_id=self.user_id)


@dataclass(frozen=True)
class PlatformClientPresence:
    session_id: str
    org_id: int
    user_id: int
    client_type: str


@dataclass(frozen=True)
class PlatformPresenceSummary:
    web_user_ids: frozenset[int]
    desktop_user_ids: frozenset[int]
    window_seconds: int
    observed_at: datetime


class PlatformSessionStore(Protocol):
    async def create(self, principal: Principal) -> PlatformSession:
        ...

    async def create_data_app_launch(
        self,
        principal: Principal,
        *,
        app_id: int,
        ttl_seconds: int,
    ) -> str:
        ...

    async def consume_data_app_launch(self, ticket: str) -> DataAppLaunch | None:
        ...

    async def create_from_enterprise_idp(
        self,
        principal: Principal,
        *,
        token_fingerprint: str,
        assertion_ttl_seconds: int,
    ) -> PlatformSession | None:
        ...

    async def get(self, session_id: str) -> PlatformSession | None:
        ...

    async def touch(self, session_id: str) -> bool:
        ...

    async def revoke(self, session_id: str) -> None:
        ...

    async def record_client_presence(
        self,
        session_id: str,
        principal: Principal,
        *,
        client_type: str,
    ) -> None:
        ...

    async def presence_summary(
        self,
        org_id: int,
        *,
        window_seconds: int,
    ) -> PlatformPresenceSummary:
        ...


class RedisPlatformSessionStore:
    def __init__(
        self,
        client: Redis,
        *,
        idle_ttl_seconds: int,
        key_prefix: str = "hermes:platform-session:",
    ) -> None:
        self.client = client
        self.idle_ttl_seconds = max(1, int(idle_ttl_seconds))
        self.key_prefix = key_prefix

    async def create(self, principal: Principal) -> PlatformSession:
        session = _new_platform_session(principal)
        try:
            await self.client.set(
                self._key(session.session_id),
                json.dumps(asdict(session), separators=(",", ":"), sort_keys=True),
                ex=self.idle_ttl_seconds,
            )
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc
        return session

    async def create_data_app_launch(
        self,
        principal: Principal,
        *,
        app_id: int,
        ttl_seconds: int,
    ) -> str:
        ticket = secrets.token_urlsafe(32)
        launch = DataAppLaunch(
            app_id=app_id,
            org_id=principal.org_id,
            user_id=principal.user_id,
        )
        try:
            await self.client.set(
                _data_app_launch_key(ticket),
                json.dumps(asdict(launch), separators=(",", ":"), sort_keys=True),
                ex=max(1, int(ttl_seconds)),
            )
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc
        return ticket

    async def consume_data_app_launch(self, ticket: str) -> DataAppLaunch | None:
        if not ticket:
            return None
        try:
            raw = await self.client.getdel(_data_app_launch_key(ticket))
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            return DataAppLaunch(
                app_id=int(payload["app_id"]),
                org_id=int(payload["org_id"]),
                user_id=int(payload["user_id"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    async def create_from_enterprise_idp(
        self,
        principal: Principal,
        *,
        token_fingerprint: str,
        assertion_ttl_seconds: int,
    ) -> PlatformSession | None:
        session = _new_platform_session(principal)
        try:
            created = await self.client.eval(
                _CREATE_ENTERPRISE_IDP_SESSION_SCRIPT,
                2,
                self._key(session.session_id),
                f"{ENTERPRISE_IDP_REPLAY_KEY_PREFIX}{token_fingerprint}",
                json.dumps(asdict(session), separators=(",", ":"), sort_keys=True),
                self.idle_ttl_seconds,
                max(1, int(assertion_ttl_seconds)),
            )
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc
        return session if int(created) == 1 else None

    async def get(self, session_id: str) -> PlatformSession | None:
        if not session_id:
            return None
        try:
            raw = await self.client.get(self._key(session_id))
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            session = PlatformSession(
                session_id=str(payload["session_id"]),
                org_id=int(payload["org_id"]),
                user_id=int(payload["user_id"]),
                created_at=int(payload["created_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not secrets.compare_digest(session.session_id, session_id):
            return None
        return session

    async def touch(self, session_id: str) -> bool:
        if not session_id:
            return False
        try:
            return bool(
                await self.client.expire(
                    self._key(session_id),
                    self.idle_ttl_seconds,
                )
            )
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc

    async def revoke(self, session_id: str) -> None:
        if not session_id:
            return
        try:
            await self.client.delete(
                self._key(session_id),
                self._client_presence_key(session_id),
            )
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc

    async def record_client_presence(
        self,
        session_id: str,
        principal: Principal,
        *,
        client_type: str,
    ) -> None:
        _require_client_type(client_type)
        presence = PlatformClientPresence(
            session_id=session_id,
            org_id=principal.org_id,
            user_id=principal.user_id,
            client_type=client_type,
        )
        try:
            await self.client.set(
                self._client_presence_key(session_id),
                json.dumps(asdict(presence), separators=(",", ":"), sort_keys=True),
                ex=self.idle_ttl_seconds,
            )
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc

    async def presence_summary(
        self,
        org_id: int,
        *,
        window_seconds: int,
    ) -> PlatformPresenceSummary:
        if window_seconds < 1:
            raise ValueError("presence window must be positive")

        keys: list[str] = []
        web_users: set[int] = set()
        desktop_users: set[int] = set()
        try:
            async for key in self.client.scan_iter(
                match=f"{self.client_presence_prefix}*",
                count=100,
            ):
                keys.append(str(key))

            for offset in range(0, len(keys), 100):
                chunk = keys[offset:offset + 100]
                pipeline = self.client.pipeline(transaction=False)
                for key in chunk:
                    pipeline.get(key)
                    pipeline.ttl(key)
                values = await pipeline.execute()

                for index, raw in enumerate(values[::2]):
                    ttl = int(values[index * 2 + 1])
                    if not isinstance(raw, str) or ttl <= 0:
                        continue
                    if self.idle_ttl_seconds - ttl > window_seconds:
                        continue
                    try:
                        payload = json.loads(raw)
                        presence = _decode_client_presence(payload)
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if presence.org_id != org_id:
                        continue
                    if presence.client_type == "web":
                        web_users.add(presence.user_id)
                    else:
                        desktop_users.add(presence.user_id)
        except RedisError as exc:
            raise PlatformSessionUnavailable("platform session store unavailable") from exc

        return PlatformPresenceSummary(
            web_user_ids=frozenset(web_users),
            desktop_user_ids=frozenset(desktop_users),
            window_seconds=window_seconds,
            observed_at=datetime.now(timezone.utc),
        )

    def _key(self, session_id: str) -> str:
        return f"{self.key_prefix}{session_id}"

    @property
    def client_presence_prefix(self) -> str:
        return "hermes:platform-client-presence:"

    def _client_presence_key(self, session_id: str) -> str:
        return f"{self.client_presence_prefix}{session_id}"


class InMemoryPlatformSessionStore:
    """Small injectable store for contract mode and focused tests."""

    def __init__(
        self,
        *,
        idle_ttl_seconds: int = 180,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.idle_ttl_seconds = max(1, int(idle_ttl_seconds))
        self.now = now
        self.sessions: dict[str, tuple[PlatformSession, float]] = {}
        self.client_presences: dict[
            str,
            tuple[PlatformClientPresence, float],
        ] = {}
        self.enterprise_idp_replays: dict[str, float] = {}
        self.data_app_launches: dict[str, tuple[DataAppLaunch, float]] = {}
        self._enterprise_idp_lock = asyncio.Lock()
        self._data_app_launch_lock = asyncio.Lock()

    async def create(self, principal: Principal) -> PlatformSession:
        session = _new_platform_session(principal, now=self.now)
        self.sessions[session.session_id] = (
            session,
            self.now() + self.idle_ttl_seconds,
        )
        return session

    async def create_data_app_launch(
        self,
        principal: Principal,
        *,
        app_id: int,
        ttl_seconds: int,
    ) -> str:
        ticket = secrets.token_urlsafe(32)
        launch = DataAppLaunch(
            app_id=app_id,
            org_id=principal.org_id,
            user_id=principal.user_id,
        )
        self.data_app_launches[_data_app_launch_key(ticket)] = (
            launch,
            self.now() + max(1, int(ttl_seconds)),
        )
        return ticket

    async def consume_data_app_launch(self, ticket: str) -> DataAppLaunch | None:
        if not ticket:
            return None
        async with self._data_app_launch_lock:
            item = self.data_app_launches.pop(_data_app_launch_key(ticket), None)
            if item is None:
                return None
            launch, expires_at = item
            return launch if expires_at > self.now() else None

    async def create_from_enterprise_idp(
        self,
        principal: Principal,
        *,
        token_fingerprint: str,
        assertion_ttl_seconds: int,
    ) -> PlatformSession | None:
        async with self._enterprise_idp_lock:
            replay_expires_at = self.enterprise_idp_replays.get(token_fingerprint)
            if replay_expires_at is not None and replay_expires_at > self.now():
                return None
            session = _new_platform_session(principal, now=self.now)
            self.sessions[session.session_id] = (
                session,
                self.now() + self.idle_ttl_seconds,
            )
            self.enterprise_idp_replays[token_fingerprint] = (
                self.now() + max(1, int(assertion_ttl_seconds))
            )
            return session

    async def get(self, session_id: str) -> PlatformSession | None:
        item = self.sessions.get(session_id)
        if item is None:
            return None
        session, expires_at = item
        if expires_at <= self.now():
            self.sessions.pop(session_id, None)
            return None
        return session

    async def touch(self, session_id: str) -> bool:
        session = await self.get(session_id)
        if session is None:
            return False
        self.sessions[session_id] = (
            session,
            self.now() + self.idle_ttl_seconds,
        )
        return True

    async def revoke(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        self.client_presences.pop(session_id, None)

    async def record_client_presence(
        self,
        session_id: str,
        principal: Principal,
        *,
        client_type: str,
    ) -> None:
        _require_client_type(client_type)
        presence = PlatformClientPresence(
            session_id=session_id,
            org_id=principal.org_id,
            user_id=principal.user_id,
            client_type=client_type,
        )
        self.client_presences[session_id] = (
            presence,
            self.now() + self.idle_ttl_seconds,
        )

    async def presence_summary(
        self,
        org_id: int,
        *,
        window_seconds: int,
    ) -> PlatformPresenceSummary:
        if window_seconds < 1:
            raise ValueError("presence window must be positive")
        now = self.now()
        web_users: set[int] = set()
        desktop_users: set[int] = set()
        for presence, expires_at in list(self.client_presences.values()):
            if expires_at <= now or presence.org_id != org_id:
                continue
            last_active_at = expires_at - self.idle_ttl_seconds
            if now - last_active_at > window_seconds:
                continue
            if presence.client_type == "web":
                web_users.add(presence.user_id)
            else:
                desktop_users.add(presence.user_id)

        return PlatformPresenceSummary(
            web_user_ids=frozenset(web_users),
            desktop_user_ids=frozenset(desktop_users),
            window_seconds=window_seconds,
            observed_at=datetime.fromtimestamp(now, timezone.utc),
        )


def _data_app_launch_key(ticket: str) -> str:
    digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    return f"{DATA_APP_LAUNCH_KEY_PREFIX}{digest}"


def _new_platform_session(
    principal: Principal,
    *,
    now: Callable[[], float] = time.time,
) -> PlatformSession:
    return PlatformSession(
        session_id=secrets.token_urlsafe(48),
        org_id=principal.org_id,
        user_id=principal.user_id,
        created_at=int(now()),
    )


def _require_client_type(client_type: str) -> None:
    if client_type not in {"web", "desktop"}:
        raise ValueError("client type must be web or desktop")


def _decode_client_presence(payload: object) -> PlatformClientPresence:
    if not isinstance(payload, dict):
        raise TypeError("presence payload must be an object")
    return PlatformClientPresence(
        session_id=str(payload["session_id"]),
        org_id=int(payload["org_id"]),
        user_id=int(payload["user_id"]),
        client_type=str(payload["client_type"]),
    )


@lru_cache(maxsize=1)
def shared_platform_session_store() -> RedisPlatformSessionStore:
    return RedisPlatformSessionStore(
        from_url(settings.REDIS_URL, decode_responses=True),
        idle_ttl_seconds=settings.PLATFORM_SESSION_IDLE_TTL_SECONDS,
    )


async def close_platform_session_store() -> None:
    if shared_platform_session_store.cache_info().currsize == 0:
        return
    store = shared_platform_session_store()
    shared_platform_session_store.cache_clear()
    await store.client.aclose()
