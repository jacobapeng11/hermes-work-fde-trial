from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from platform_supervisor.execution import (
    PrepareIdentity,
    SessionCapacityBusy,
    SessionManager,
)
from platform_supervisor.storage import SQLiteStore


def _identity() -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id="runtime-1",
        session_id="session-1",
        profile_id="expert-1",
        model_id="7",
        config_revision="revision-1",
        admission_generation=1,
    )


async def _manager(
    tmp_path: Path,
    *,
    prepare,
    close,
    refresh=None,
) -> tuple[SQLiteStore, SessionManager]:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    sessions = SessionManager(
        store,
        runtime_id="runtime-1",
        max_inflight_runs=2,
        max_resident_sessions=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
        profile_refresh_callback=refresh,
    )
    return store, sessions


@pytest.mark.asyncio
async def test_profile_revision_change_refreshes_persisted_prompt_before_resume(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, int]] = []

    async def prepare(identity: PrepareIdentity) -> None:
        calls.append(("prepare", identity.profile_applied_revision))

    async def close(identity: PrepareIdentity) -> None:
        calls.append(("close", identity.profile_applied_revision))

    async def refresh(identity: PrepareIdentity) -> None:
        calls.append(("refresh", identity.profile_applied_revision))

    store, sessions = await _manager(
        tmp_path,
        prepare=prepare,
        close=close,
        refresh=refresh,
    )
    old = replace(
        _identity(),
        runtime_agent_profile_id=1,
        profile_desired_revision=1,
        profile_applied_revision=1,
        profile_digest="sha256:old",
    )
    current = replace(
        old,
        profile_desired_revision=2,
        profile_applied_revision=2,
        profile_digest="sha256:new",
    )

    await sessions.prepare(old)
    await sessions.close()
    restarted = SessionManager(
        store,
        runtime_id="runtime-1",
        max_inflight_runs=2,
        max_resident_sessions=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
        profile_refresh_callback=refresh,
    )

    await restarted.prepare(current)

    assert calls == [
        ("prepare", 1),
        ("close", 1),
        ("refresh", 2),
        ("prepare", 2),
    ]
    await restarted.close()
    await store.close()


@pytest.mark.asyncio
async def test_application_config_revision_refreshes_prompt_on_next_prepare(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def prepare(_: PrepareIdentity) -> None:
        calls.append("prepare")

    async def close(_: PrepareIdentity) -> None:
        calls.append("close")

    async def refresh(_: PrepareIdentity) -> None:
        calls.append("refresh")

    store, sessions = await _manager(
        tmp_path,
        prepare=prepare,
        close=close,
        refresh=refresh,
    )
    old = replace(
        _identity(),
        profile_id="default",
        runtime_agent_profile_id=0,
    )
    current = replace(old, config_revision="generation-2")

    await sessions.prepare(old)
    await sessions.close()
    restarted = SessionManager(
        store,
        runtime_id="runtime-1",
        max_inflight_runs=2,
        max_resident_sessions=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
        profile_refresh_callback=refresh,
    )

    await restarted.prepare(current)

    assert calls == ["prepare", "close", "refresh", "prepare"]
    await restarted.close()
    await store.close()


def test_personal_config_revision_does_not_replace_profile_prompt_identity() -> None:
    old = replace(
        _identity(),
        runtime_agent_profile_id=7,
        config_revision="generation-1",
    )
    current = replace(old, config_revision="generation-2")

    assert not SessionManager._profile_revision_changed(
        type(
            "Envelope",
            (),
            {"prepare_identity": SessionManager._identity_payload(old)},
        )(),
        current,
    )


@pytest.mark.asyncio
async def test_reload_generation_closes_idle_agent_and_forces_resume(
    tmp_path: Path,
) -> None:
    prepared: list[str] = []
    closed: list[str] = []

    async def prepare(identity: PrepareIdentity) -> None:
        prepared.append(identity.session_id)

    async def close(identity: PrepareIdentity) -> None:
        closed.append(identity.session_id)

    store, sessions = await _manager(tmp_path, prepare=prepare, close=close)
    identity = _identity()
    await sessions.prepare(identity)

    marked = await sessions.advance_agent_generation("mcp-reload")

    assert marked == 1
    assert closed == [identity.session_id]
    assert sessions.resident_count == 0
    await sessions.prepare(identity)
    assert prepared == [identity.session_id, identity.session_id]
    assert (await sessions.metrics_snapshot())["agent_generation"] == 1

    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_reload_generation_defers_active_agent_close_until_run_terminal(
    tmp_path: Path,
) -> None:
    prepared: list[str] = []
    closed: list[str] = []

    async def prepare(identity: PrepareIdentity) -> None:
        prepared.append(identity.session_id)

    async def close(identity: PrepareIdentity) -> None:
        closed.append(identity.session_id)

    store, sessions = await _manager(tmp_path, prepare=prepare, close=close)
    identity = _identity()
    await sessions.prepare(identity)
    await sessions.mark_run_started(identity)

    marked = await sessions.advance_agent_generation("mcp-reload")

    assert marked == 1
    assert closed == []
    assert sessions.resident_count == 1
    assert (await sessions.metrics_snapshot())["stale_agents"] == 1

    await sessions.mark_run_terminal(identity.session_id)

    assert closed == [identity.session_id]
    assert sessions.resident_count == 0
    await sessions.prepare(identity)
    assert prepared == [identity.session_id, identity.session_id]

    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_prepare_crossing_reload_generation_retries_before_returning(
    tmp_path: Path,
) -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    prepare_count = 0
    closed: list[str] = []

    async def prepare(identity: PrepareIdentity) -> None:
        nonlocal prepare_count
        prepare_count += 1
        if prepare_count == 1:
            first_entered.set()
            await release_first.wait()

    async def close(identity: PrepareIdentity) -> None:
        closed.append(identity.session_id)

    store, sessions = await _manager(tmp_path, prepare=prepare, close=close)
    identity = _identity()
    task = asyncio.create_task(sessions.prepare(identity))
    await first_entered.wait()

    marked = await sessions.advance_agent_generation("mcp-reload")
    release_first.set()
    prepared = await task

    assert marked == 1
    assert prepare_count == 2
    assert closed == [identity.session_id]
    assert prepared.agent_generation == 1

    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_reload_between_prepare_and_run_start_requeues_instead_of_running_stale(
    tmp_path: Path,
) -> None:
    async def prepare(_: PrepareIdentity) -> None:
        return None

    async def close(_: PrepareIdentity) -> None:
        return None

    store, sessions = await _manager(tmp_path, prepare=prepare, close=close)
    identity = _identity()
    await sessions.prepare(identity)
    await sessions.advance_agent_generation("mcp-reload")

    with pytest.raises(SessionCapacityBusy):
        await sessions.mark_run_started(identity)

    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_prepare_waits_for_reload_idle_agent_close_before_resuming(
    tmp_path: Path,
) -> None:
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    second_prepare_entered = asyncio.Event()
    events: list[str] = []

    async def prepare(_: PrepareIdentity) -> None:
        events.append("prepare")
        if events.count("prepare") == 2:
            second_prepare_entered.set()

    async def close(_: PrepareIdentity) -> None:
        events.append("close-start")
        close_entered.set()
        await release_close.wait()
        events.append("close-done")

    store, sessions = await _manager(tmp_path, prepare=prepare, close=close)
    identity = _identity()
    await sessions.prepare(identity)

    advance = asyncio.create_task(
        sessions.advance_agent_generation("mcp-reload")
    )
    await close_entered.wait()
    resume = asyncio.create_task(sessions.prepare(identity))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.shield(second_prepare_entered.wait()),
            timeout=0.05,
        )
    assert events == ["prepare", "close-start"]

    release_close.set()
    await asyncio.gather(advance, resume)
    assert events == ["prepare", "close-start", "close-done", "prepare"]

    await sessions.close()
    await store.close()
