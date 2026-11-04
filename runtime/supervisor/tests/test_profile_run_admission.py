from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from platform_supervisor.execution import (
    CapacityGate,
    PrepareIdentity,
    RunManager,
    SessionManager,
)
from platform_supervisor.asset_contracts import RuntimeMcpRegistryApplyRequest
from platform_supervisor.hermes.events import PlatformStreamEvent
from platform_supervisor.mcp_registry import McpRegistryExecutionGate
from platform_supervisor.mcp_registry_state import McpRegistryStateStore
from platform_supervisor.storage import SQLiteStore


def _digest(marker: str) -> str:
    return f"sha256:{marker * 64}"


def _identity() -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id="runtime-119",
        session_id="session-alpha",
        profile_id="expert-173",
        runtime_agent_profile_id=173,
        model_id="7",
        config_revision="runtime-generation-1",
        admission_generation=11,
        profile_desired_revision=1,
        profile_applied_revision=1,
        profile_digest=_digest("a"),
        skill_digest=_digest("b"),
        mcp_digest=_digest("c"),
        knowledge_digest=_digest("d"),
    )


async def _wait_until(predicate, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not become true")


class MutableProfileIdentityResolver:
    def __init__(self) -> None:
        self.ready = True
        self.revision = 1
        self.calls = 0

    async def __call__(
        self,
        identity: PrepareIdentity,
    ) -> PrepareIdentity | None:
        self.calls += 1
        if not self.ready:
            return None
        marker = str(self.revision % 10)
        return replace(
            identity,
            profile_desired_revision=self.revision,
            profile_applied_revision=self.revision,
            profile_digest=_digest(marker),
            skill_digest=_digest(marker),
            mcp_digest=_digest(marker),
            knowledge_digest=_digest(marker),
        )


class RecordingAdapter:
    def __init__(self) -> None:
        self.identities: list[PrepareIdentity] = []
        self.releases: dict[str, asyncio.Event] = {}

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del prompt, trusted_context, runtime_provider_run_id_sink
        self.identities.append(identity)
        if prompt_submit_started_sink is not None:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        if prompt_ack_sink is not None:
            await prompt_ack_sink()
        yield PlatformStreamEvent("chat.run.started", {"run_id": run_id})
        await self.releases.setdefault(run_id, asyncio.Event()).wait()
        yield PlatformStreamEvent("chat.message.completed", {"text": "done"})
        yield PlatformStreamEvent("chat.done", {"run_id": run_id})

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        del identity, runtime_provider_run_id


async def _build_manager(tmp_path: Path):
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    prepared: list[PrepareIdentity] = []
    closed: list[PrepareIdentity] = []

    async def prepare(identity: PrepareIdentity) -> None:
        prepared.append(identity)

    async def close(identity: PrepareIdentity) -> None:
        closed.append(identity)

    sessions = SessionManager(
        store,
        runtime_id="runtime-119",
        max_inflight_runs=2,
        max_resident_sessions=3,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    resolver = MutableProfileIdentityResolver()
    adapter = RecordingAdapter()
    manager = RunManager(
        store,
        sessions,
        CapacityGate(2),
        adapter,
        profile_identity_resolver=resolver,
    )
    return store, sessions, manager, resolver, adapter, prepared, closed


@pytest.mark.asyncio
async def test_new_run_waits_until_profile_projection_is_applied(
    tmp_path: Path,
) -> None:
    store, sessions, manager, resolver, adapter, prepared, _closed = (
        await _build_manager(tmp_path)
    )
    resolver.ready = False
    submission = await manager.submit(
        identity=_identity(),
        client_message_id="message-waits-for-assets",
        payload_digest="a" * 64,
        prompt="use the new skill",
        source="web",
    )
    queued = await store.get_run(submission.run.run_id)
    assert queued is not None and queued.state == "QUEUED"
    assert adapter.identities == []
    assert prepared == []

    resolver.revision = 2
    resolver.ready = True
    await manager.wake_queued_sessions()
    await _wait_until(lambda: len(adapter.identities) == 1)
    effective = adapter.identities[0]
    assert effective.profile_applied_revision == 2
    assert effective.skill_digest == _digest("2")
    assert prepared[-1] == effective

    await _wait_until(lambda: submission.run.run_id in adapter.releases)
    adapter.releases[submission.run.run_id].set()
    await _wait_until(
        lambda: submission.run.run_id not in manager._contexts
    )
    completed = await store.get_run(submission.run.run_id)
    assert completed is not None and completed.state == "COMPLETED"
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_new_run_waits_during_global_mcp_reload_then_dispatches(
    tmp_path: Path,
) -> None:
    store, sessions, manager, resolver, adapter, prepared, _closed = (
        await _build_manager(tmp_path)
    )
    registry_state = McpRegistryStateStore(tmp_path / "mcp-registry.db")
    await registry_state.start()
    request = RuntimeMcpRegistryApplyRequest(
        operation_id="registry-operation-1",
        runtime_id="runtime-119",
        binding_id=119,
        runtime_workspace_id=119,
        desired_revision=1,
        expected_applied_revision=0,
        registry_digest="sha256:" + "f" * 64,
        servers=[],
    )
    await registry_state.begin(request, affected_run_ids=["run-already-working"])
    manager.profile_identity_resolver = McpRegistryExecutionGate(
        state=registry_state,
        resolver=resolver,
    )

    submission = await manager.submit(
        identity=_identity(),
        client_message_id="message-during-mcp-reload",
        payload_digest="9" * 64,
        prompt="wait for registry reload",
        source="web",
    )
    queued = await store.get_run(submission.run.run_id)
    assert queued is not None and queued.state == "QUEUED"
    assert adapter.identities == []
    assert prepared == []

    await registry_state.finish(request, status="ready", servers=[])
    await manager.wake_queued_sessions()
    await _wait_until(lambda: len(adapter.identities) == 1)
    assert adapter.identities[0].session_id == "session-alpha"

    await _wait_until(lambda: submission.run.run_id in adapter.releases)
    adapter.releases[submission.run.run_id].set()
    await _wait_until(lambda: submission.run.run_id not in manager._contexts)
    await manager.close()
    await sessions.close()
    await store.close()
    await registry_state.close()


@pytest.mark.asyncio
async def test_active_run_finishes_old_revision_then_queued_run_rebuilds_latest(
    tmp_path: Path,
) -> None:
    store, sessions, manager, resolver, adapter, prepared, closed = (
        await _build_manager(tmp_path)
    )
    selected = _identity()
    first = await manager.submit(
        identity=selected,
        client_message_id="message-old-revision",
        payload_digest="b" * 64,
        prompt="finish with old assets",
        source="web",
    )
    await _wait_until(lambda: len(adapter.identities) == 1)
    assert adapter.identities[0].profile_applied_revision == 1

    resolver.revision = 2
    second = await manager.submit(
        identity=selected,
        client_message_id="message-new-revision",
        payload_digest="c" * 64,
        prompt="use new assets",
        source="web",
    )
    queued = await store.get_run(second.run.run_id)
    assert queued is not None and queued.state == "QUEUED"
    assert len(adapter.identities) == 1
    assert closed == []

    await _wait_until(lambda: first.run.run_id in adapter.releases)
    adapter.releases[first.run.run_id].set()
    await _wait_until(lambda: len(adapter.identities) == 2)
    assert adapter.identities[1].profile_applied_revision == 2
    assert adapter.identities[1].skill_digest == _digest("2")
    assert [item.profile_applied_revision for item in prepared] == [1, 2]
    assert [item.profile_applied_revision for item in closed] == [1]

    first_completed = await store.get_run(first.run.run_id)
    assert first_completed is not None and first_completed.state == "COMPLETED"
    await _wait_until(lambda: second.run.run_id in adapter.releases)
    adapter.releases[second.run.run_id].set()
    await _wait_until(lambda: second.run.run_id not in manager._contexts)
    second_completed = await store.get_run(second.run.run_id)
    assert second_completed is not None and second_completed.state == "COMPLETED"
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_profile_rebuild_is_lazy_per_session_and_does_not_close_beta(
    tmp_path: Path,
) -> None:
    store, sessions, manager, _resolver, _adapter, prepared, closed = (
        await _build_manager(tmp_path)
    )
    alpha_one = _identity()
    alpha_two = replace(alpha_one, session_id="session-alpha-two")
    beta = replace(
        alpha_one,
        session_id="session-beta",
        profile_id="expert-174",
        runtime_agent_profile_id=174,
    )
    await sessions.prepare(alpha_one)
    await sessions.prepare(alpha_two)
    await sessions.prepare(beta)

    alpha_one_v2 = replace(
        alpha_one,
        profile_desired_revision=2,
        profile_applied_revision=2,
        profile_digest=_digest("2"),
    )
    await sessions.prepare(alpha_one_v2)
    assert [item.session_id for item in closed] == ["session-alpha"]
    assert prepared[-1] == alpha_one_v2
    assert all(item.runtime_agent_profile_id != 174 for item in closed)

    alpha_two_v2 = replace(alpha_one_v2, session_id="session-alpha-two")
    await sessions.prepare(alpha_two_v2)
    assert [item.session_id for item in closed] == [
        "session-alpha",
        "session-alpha-two",
    ]
    assert all(item.runtime_agent_profile_id != 174 for item in closed)
    await manager.close()
    await sessions.close()
    await store.close()
