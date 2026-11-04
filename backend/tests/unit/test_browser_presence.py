from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.api.v1.user.browser_presence import _presence_events, _resolve_client_type
from app.config import settings
from app.services.platform_presence import ContractCrossDayPresenceRecorder
from app.services.platform_session import InMemoryPlatformSessionStore
from app.services.runtime_workspace import Principal


class RecordingRuntimeProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def touch(self, _principal: Principal) -> str:
        self.calls += 1
        return "ready"


async def test_presence_stream_renews_session_and_touches_runtime() -> None:
    original_heartbeat = settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS
    original_touch = settings.BROWSER_PRESENCE_RUNTIME_TOUCH_SECONDS
    settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = 0.001
    settings.BROWSER_PRESENCE_RUNTIME_TOUCH_SECONDS = 0.001
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=30)
    principal = Principal(org_id=1, user_id=7)
    session = await store.create(principal)
    probe = RecordingRuntimeProbe()
    events = _presence_events(
        principal=principal,
        session_id=session.session_id,
        session_store=store,
        runtime_probe=probe,
        client_type="web",
        session_created_at=None,
        cross_day_recorder=ContractCrossDayPresenceRecorder(),
    )
    try:
        connected = await anext(events)
        first_heartbeat = await anext(events)
        await asyncio.sleep(0.003)
        second_heartbeat = await anext(events)
    finally:
        await events.aclose()
        settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = original_heartbeat
        settings.BROWSER_PRESENCE_RUNTIME_TOUCH_SECONDS = original_touch

    await asyncio.sleep(0)
    assert connected.startswith(": connected ")
    assert "event: presence.heartbeat" in first_heartbeat
    assert '"session":"active"' in second_heartbeat
    assert probe.calls >= 2
    assert await store.get(session.session_id) is not None
    assert store.client_presences[session.session_id][0].client_type == "web"


async def test_presence_stream_keeps_auth_alive_when_runtime_is_unavailable() -> None:
    class UnavailableRuntimeProbe:
        async def touch(self, _principal: Principal) -> str:
            return "unavailable"

    original_heartbeat = settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS
    settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = 0.001
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=30)
    principal = Principal(org_id=1, user_id=7)
    session = await store.create(principal)
    events = _presence_events(
        principal=principal,
        session_id=session.session_id,
        session_store=store,
        runtime_probe=UnavailableRuntimeProbe(),
        client_type="desktop",
        session_created_at=None,
        cross_day_recorder=ContractCrossDayPresenceRecorder(),
    )
    try:
        await anext(events)
        await anext(events)
        await asyncio.sleep(0.003)
        heartbeat = await anext(events)
    finally:
        await events.aclose()
        settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = original_heartbeat

    assert '"runtime":"unavailable"' in heartbeat
    assert '"session":"active"' in heartbeat
    assert store.client_presences[session.session_id][0].client_type == "desktop"


def test_presence_client_type_prefers_explicit_header_and_falls_back_to_electron() -> None:
    assert _resolve_client_type("desktop", "Mozilla/5.0") == "desktop"
    assert _resolve_client_type("web", "Mozilla/5.0 Electron/44") == "web"
    assert _resolve_client_type(None, "Mozilla/5.0 Electron/44") == "desktop"
    assert _resolve_client_type(None, "Mozilla/5.0 Chrome") == "web"


async def test_cross_day_presence_is_recorded_once_runtime_is_ready() -> None:
    class RecordingCrossDayRecorder:
        def __init__(self) -> None:
            self.calls = 0

        async def record(self, _principal: Principal) -> bool:
            self.calls += 1
            return True

    original_heartbeat = settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS
    settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = 0.001
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=30)
    principal = Principal(org_id=1, user_id=7)
    session = await store.create(principal)
    created_at = int(
        (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    )
    recorder = RecordingCrossDayRecorder()
    events = _presence_events(
        principal=principal,
        session_id=session.session_id,
        session_store=store,
        runtime_probe=RecordingRuntimeProbe(),
        client_type="web",
        session_created_at=created_at,
        cross_day_recorder=recorder,
    )
    try:
        await anext(events)
        await anext(events)
        await asyncio.sleep(0.003)
        await anext(events)
        await asyncio.sleep(0.003)
        await anext(events)
    finally:
        await events.aclose()
        settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = original_heartbeat

    assert recorder.calls == 1


async def test_cross_day_presence_is_not_recorded_when_runtime_is_unavailable() -> None:
    class UnavailableRuntimeProbe:
        async def touch(self, _principal: Principal) -> str:
            return "unavailable"

    class RecordingCrossDayRecorder:
        def __init__(self) -> None:
            self.calls = 0

        async def record(self, _principal: Principal) -> bool:
            self.calls += 1
            return True

    original_heartbeat = settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS
    settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = 0.001
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=30)
    principal = Principal(org_id=1, user_id=7)
    session = await store.create(principal)
    created_at = int(
        (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    )
    recorder = RecordingCrossDayRecorder()
    events = _presence_events(
        principal=principal,
        session_id=session.session_id,
        session_store=store,
        runtime_probe=UnavailableRuntimeProbe(),
        client_type="web",
        session_created_at=created_at,
        cross_day_recorder=recorder,
    )
    try:
        await anext(events)
        await anext(events)
        await asyncio.sleep(0.003)
        await anext(events)
    finally:
        await events.aclose()
        settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS = original_heartbeat

    assert recorder.calls == 0
