from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from platform_supervisor.execution.manager import (
    PER_RUN_REALTIME_LIMIT,
    _RunContext,
    _Subscriber,
    RunSubmission,
)
from platform_supervisor.execution import (
    CapacityGate,
    PrepareIdentity,
    RunManager,
    SessionCapacityBusy,
    SessionManager,
)
from platform_supervisor.hermes.events import PlatformStreamEvent
from platform_supervisor.hermes.skill_manage_observation import (
    ObservedSkillManageCompletion,
)
from platform_supervisor.storage import (
    DispatchIntent,
    IdempotencyConflict,
    RunEnqueueIntent,
    SQLiteStore,
    StoredRedirect,
)


def identity(index: int) -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id="application-runtime-1",
        session_id=f"session-{index:08d}",
        profile_id="default",
        model_id="7",
        config_revision="revision-18",
        admission_generation=11,
    )


def test_prepare_identity_ignores_random_capsule_but_binds_route_digest() -> None:
    route_digest = "sha256:" + "a" * 64
    first = replace(
        identity(1),
        provider_route={
            "version": 1,
            "capsule": "capsule-first",
            "digest": route_digest,
        },
        provider_route_digest=route_digest,
    )
    refreshed = replace(
        identity(1),
        provider_route={
            "version": 1,
            "capsule": "capsule-refreshed",
            "digest": route_digest,
        },
        provider_route_digest=route_digest,
    )
    rotated = replace(
        identity(1),
        provider_route={
            "version": 1,
            "capsule": "capsule-rotated",
            "digest": "sha256:" + "b" * 64,
        },
        provider_route_digest="sha256:" + "b" * 64,
    )

    assert first == refreshed
    assert first.digest == refreshed.digest
    assert first != rotated
    assert first.digest != rotated.digest


async def wait_until(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


class FakeAdapter:
    def __init__(self) -> None:
        self.release_by_session: dict[str, asyncio.Event] = {}
        self.stream_calls: list[str] = []
        self.cancel_calls: list[tuple[str, str | None]] = []
        self.canceled_runs: set[str] = set()
        self.provider_run_ids: dict[str, str] = {}

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del prompt, trusted_context, attachments
        self.stream_calls.append(identity.session_id)
        if runtime_provider_run_id_sink:
            await runtime_provider_run_id_sink(
                self.provider_run_ids.get(run_id, run_id)
            )
        if prompt_submit_started_sink:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        if prompt_ack_sink:
            await prompt_ack_sink()
        yield PlatformStreamEvent(
            "chat.run.started",
            {"run_id": self.provider_run_ids.get(run_id, run_id)},
        )
        release = self.release_by_session.setdefault(identity.session_id, asyncio.Event())
        live_id = self.provider_run_ids.get(run_id, run_id)
        await release.wait()
        yield PlatformStreamEvent("chat.message.delta", {"delta": "完成", "source": "agent"})
        yield PlatformStreamEvent("chat.message.completed", {"text": "完成"})
        yield PlatformStreamEvent(
            "chat.done",
            {
                "run_id": run_id,
                "status": (
                    "canceled"
                    if live_id in self.canceled_runs
                    else "completed"
                ),
            },
        )

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        self.cancel_calls.append((identity.session_id, runtime_provider_run_id))
        if runtime_provider_run_id is not None:
            self.canceled_runs.add(runtime_provider_run_id)
        self.release_by_session.setdefault(identity.session_id, asyncio.Event()).set()


class TerminalLeaseAdapter:
    """Keeps the Dashboard-style Session lease until its stream is closed."""

    def __init__(self) -> None:
        self.in_use = False
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.stream_calls: list[str] = []

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del prompt, trusted_context, attachments
        if self.in_use:
            raise RuntimeError(
                "runtime dashboard Session already has an active run"
            )
        self.in_use = True
        self.stream_calls.append(run_id)
        try:
            if runtime_provider_run_id_sink:
                await runtime_provider_run_id_sink(run_id)
            if prompt_submit_started_sink:
                started = prompt_submit_started_sink()
                if started is not None:
                    await started
            if prompt_ack_sink:
                await prompt_ack_sink()
            yield PlatformStreamEvent("chat.run.started", {"run_id": run_id})
            if len(self.stream_calls) == 1:
                self.first_started.set()
                await self.release_first.wait()
            yield PlatformStreamEvent(
                "chat.message.completed",
                {"text": "完成"},
            )
            yield PlatformStreamEvent("chat.done", {"run_id": run_id})
        finally:
            self.in_use = False

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        del identity, runtime_provider_run_id
        self.release_first.set()


class FailingAdapter:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del identity, run_id, prompt, trusted_context, attachments
        del runtime_provider_run_id_sink
        await self.release.wait()
        if self.stage == "before_submit":
            raise RuntimeError("failed before prompt submit")
        if prompt_submit_started_sink:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        if self.stage == "before_ack":
            raise RuntimeError("failed before prompt ack")
        if prompt_ack_sink:
            await prompt_ack_sink()
        if self.stage == "after_ack":
            raise RuntimeError("failed after prompt ack")
        yield PlatformStreamEvent("chat.message.delta", {"delta": "首个 Token"})
        raise RuntimeError("failed after first token")

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        del runtime_provider_run_id
        del identity
        self.release.set()


class FakeModelRouter:
    def __init__(self) -> None:
        self.activated: list[tuple[str, str]] = []
        self.revoked: list[str] = []
        self.finished: list[tuple[str, str, bool]] = []

    async def activate(self, identity: PrepareIdentity, run_id: str) -> None:
        self.activated.append((identity.session_id, run_id))

    async def revoke_run(self, run_id: str) -> None:
        self.revoked.append(run_id)

    async def finish_run(
        self,
        identity: PrepareIdentity,
        run_id: str,
        *,
        completed: bool,
    ) -> None:
        self.finished.append((identity.session_id, run_id, completed))


class RecordingRedirectLifecycle:
    def __init__(self) -> None:
        self.canceled: list[str] = []

    async def redirect_admitted(self, parent_run_id: str) -> None:
        del parent_run_id

    async def reconcile_redirect(self, parent_run_id: str) -> bool:
        del parent_run_id
        return False

    async def cancel_parent_pre_rpc(self, parent_run_id: str) -> bool:
        self.canceled.append(parent_run_id)
        return True


class TerminalFailingRedirectLifecycle(RecordingRedirectLifecycle):
    def __init__(self, store: SQLiteStore) -> None:
        super().__init__()
        self.store = store
        self.terminal_prepared: list[str] = []

    async def prepare_parent_terminal(self, parent_run_id: str) -> None:
        self.terminal_prepared.append(parent_run_id)
        parent = await self.store.get_run(parent_run_id)
        assert parent is not None
        for reservation in await self.store.list_input_admission_reservations(
            parent.session_id
        ):
            if reservation.kind != "redirect" or reservation.state == "promoted":
                break
            redirect = await self.store.get_redirect_by_message(
                session_id=parent.session_id,
                client_message_id=reservation.client_message_id,
            )
            assert redirect is not None
            if redirect.parent_run_id != parent_run_id:
                break
            if redirect.state == "WAITING_AGENT_READY":
                await self.store.mark_redirect_boundary_ready(
                    redirect.redirect_id,
                    updated_at=datetime.now(timezone.utc),
                )
        group = await self.store.prepare_redirect_group(
            dispatch_group_id=f"terminal-group-{parent_run_id}",
            parent_run_id=parent_run_id,
            created_at=datetime.now(timezone.utc),
        )
        assert group is not None
        await self.store.finalize_redirect_group_failed(
            group.dispatch_group_id,
            finalized_at=datetime.now(timezone.utc),
            error_code="FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY",
            include_all_parent_followups=True,
        )


class ControllableCancelAdapter:
    def __init__(
        self,
        *,
        publish_provider_before_prompt: bool,
    ) -> None:
        self.publish_provider_before_prompt = publish_provider_before_prompt
        self.entered = asyncio.Event()
        self.allow_prompt = asyncio.Event()
        self.release = asyncio.Event()
        self.prompt_started = False
        self.cancel_calls: list[tuple[str, str | None]] = []
        self.provider_sink = None
        self.cancel_fails = False

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del run_id, prompt, trusted_context, attachments, prompt_ack_sink
        self.provider_sink = runtime_provider_run_id_sink
        self.entered.set()
        await self.allow_prompt.wait()
        if self.publish_provider_before_prompt and runtime_provider_run_id_sink:
            await runtime_provider_run_id_sink(
                f"live-{identity.session_id}"
            )
        if prompt_submit_started_sink:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
            self.prompt_started = True
        await self.release.wait()
        yield PlatformStreamEvent(
            "chat.done",
            {
                "run_id": f"live-{identity.session_id}",
                "status": "canceled",
            },
        )

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        self.cancel_calls.append(
            (identity.session_id, runtime_provider_run_id)
        )
        if self.cancel_fails:
            raise RuntimeError("interrupt failed")
        self.release.set()


class MultiSessionCancelAdapter:
    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.canceled: set[str] = set()
        self.cancel_calls: list[tuple[str, str | None]] = []

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del run_id, prompt, trusted_context, attachments
        live_id = f"live-{identity.session_id}"
        if runtime_provider_run_id_sink:
            await runtime_provider_run_id_sink(live_id)
        if prompt_submit_started_sink:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        if prompt_ack_sink:
            await prompt_ack_sink()
        self.started.setdefault(identity.session_id, asyncio.Event()).set()
        yield PlatformStreamEvent(
            "chat.message.delta",
            {"delta": "部分回答", "source": "agent"},
        )
        await self.release.setdefault(
            identity.session_id,
            asyncio.Event(),
        ).wait()
        if identity.session_id in self.canceled:
            yield PlatformStreamEvent(
                "chat.message.completed",
                {"text": "部分回答", "status": "interrupted"},
            )
            yield PlatformStreamEvent(
                "chat.done",
                {"run_id": live_id, "status": "canceled"},
            )
            return
        yield PlatformStreamEvent(
            "chat.message.completed",
            {"text": "完整回答", "status": "complete"},
        )
        yield PlatformStreamEvent("chat.done", {"run_id": live_id})

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        self.cancel_calls.append(
            (identity.session_id, runtime_provider_run_id)
        )
        assert runtime_provider_run_id == f"live-{identity.session_id}"
        self.canceled.add(identity.session_id)
        self.release.setdefault(identity.session_id, asyncio.Event()).set()


class TenSessionIsolationAdapter:
    def __init__(self) -> None:
        self.release: dict[str, asyncio.Event] = {}
        self.seen: dict[str, tuple[PrepareIdentity, str, str]] = {}
        self.canceled: set[str] = set()
        self.cancel_calls: list[tuple[str, str | None]] = []

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del trusted_context, attachments
        live_id = f"live-{identity.session_id}-{run_id}"
        self.seen[identity.session_id] = (identity, prompt, run_id)
        if runtime_provider_run_id_sink:
            await runtime_provider_run_id_sink(live_id)
        if prompt_submit_started_sink:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        if prompt_ack_sink:
            await prompt_ack_sink()
        yield PlatformStreamEvent(
            "chat.run.started",
            {"run_id": live_id, "session_id": identity.session_id},
        )
        await self.release.setdefault(
            identity.session_id,
            asyncio.Event(),
        ).wait()
        answer = "|".join(
            (
                identity.runtime_id,
                identity.session_id,
                identity.profile_id,
                identity.model_id,
                prompt,
            )
        )
        yield PlatformStreamEvent(
            "chat.message.delta",
            {"delta": answer, "session_id": identity.session_id},
        )
        yield PlatformStreamEvent(
            "chat.message.completed",
            {"text": answer, "session_id": identity.session_id},
        )
        yield PlatformStreamEvent(
            "chat.done",
            {
                "run_id": live_id,
                "session_id": identity.session_id,
                "status": (
                    "canceled"
                    if identity.session_id in self.canceled
                    else "completed"
                ),
            },
        )

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        self.cancel_calls.append(
            (identity.session_id, runtime_provider_run_id)
        )
        self.canceled.add(identity.session_id)
        self.release.setdefault(identity.session_id, asyncio.Event()).set()


async def build_manager(
    tmp_path: Path,
    *,
    limit: int = 2,
    capacity_busy_code: str = "APPLICATION_CAPACITY_BUSY",
) -> tuple[SQLiteStore, SessionManager, CapacityGate, FakeAdapter, RunManager]:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    prepared: list[str] = []

    async def prepare(item: PrepareIdentity) -> None:
        prepared.append(item.session_id)

    async def close(_: PrepareIdentity) -> None:
        return None

    sessions = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=limit,
        max_resident_sessions=limit + 1,
        post_run_warm_grace_seconds=600,
        hot_session_limit=1,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    capacity = CapacityGate(limit, busy_code=capacity_busy_code)
    adapter = FakeAdapter()
    manager = RunManager(store, sessions, capacity, adapter)
    return store, sessions, capacity, adapter, manager


@pytest.mark.asyncio
async def test_interactive_text_uses_durable_redirect_disposition(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-0001",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: parent.run.run_id in manager._contexts)
    await wait_until(
        lambda: selected.session_id in adapter.release_by_session
    )

    redirect = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-correction-0001",
        payload_digest="b" * 64,
        prompt="please correct that",
        attachment_file_ids=(),
        redirect_eligible=True,
        admission_generation=11,
    )
    assert isinstance(redirect, StoredRedirect)
    assert redirect.parent_run_id == parent.run.run_id
    assert redirect.state == "WAITING_AGENT_READY"
    assert redirect.pending_event_seq > 0

    replay = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-correction-0001",
        payload_digest="b" * 64,
        prompt="please correct that",
        attachment_file_ids=(),
        redirect_eligible=True,
        admission_generation=11,
    )
    assert isinstance(replay, StoredRedirect)
    assert replay.redirect_id == redirect.redirect_id
    assert replay.pending_event_seq == redirect.pending_event_seq

    with pytest.raises(PermissionError, match="ADMISSION_GENERATION_MISMATCH"):
        await manager.submit_interactive(
            identity=selected,
            client_message_id="message-correction-stale",
            payload_digest="c" * 64,
            prompt="stale",
            admission_generation=10,
        )

    await wait_until(lambda: selected.session_id in adapter.release_by_session)
    adapter.release_by_session[selected.session_id].set()
    task = manager._contexts[parent.run.run_id].task
    assert task is not None
    await task
    promoted = await store.get_redirect_by_message(
        session_id=selected.session_id,
        client_message_id="message-correction-0001",
    )
    assert promoted is not None
    assert promoted.state == "PROMOTED_TO_RUN"
    assert promoted.promoted_run_id == promoted.promotion_reserved_run_id
    promoted_run = None
    for _ in range(100):
        promoted_run = await store.get_run(promoted.promoted_run_id)
        if promoted_run is not None and promoted_run.state == "COMPLETED":
            break
        await asyncio.sleep(0)
    assert promoted_run is not None
    assert promoted_run.state == "COMPLETED"
    assert adapter.stream_calls == [selected.session_id, selected.session_id]
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_interrupt_submission_replaces_active_parent_without_duplicate(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-replace-0001",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: parent.run.run_id in manager._contexts)
    await wait_until(
        lambda: selected.session_id in adapter.release_by_session
    )

    replacement = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-replacement-0001",
        payload_digest="b" * 64,
        prompt="stop and answer this instead",
        attachment_file_ids=(),
        redirect_eligible=True,
        expected_parent_run_id=parent.run.run_id,
        admission_generation=11,
        interrupt_parent=True,
    )

    assert isinstance(replacement, RunSubmission)
    assert replacement.created is True
    assert replacement.run.state == "QUEUED"
    assert replacement.run.source_detail == {
        "replacement_parent_run_id": parent.run.run_id
    }
    await wait_until(
        lambda: adapter.cancel_calls
        == [(selected.session_id, parent.run.run_id)],
        attempts=1000,
    )
    parent_task = manager._contexts[parent.run.run_id].task
    assert parent_task is not None
    await parent_task
    parent_stored = await store.get_run(parent.run.run_id)
    assert parent_stored is not None
    assert parent_stored.state == "CANCELED"

    repeated = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-replacement-0001",
        payload_digest="b" * 64,
        prompt="stop and answer this instead",
        attachment_file_ids=(),
        redirect_eligible=True,
        expected_parent_run_id=parent.run.run_id,
        admission_generation=11,
        interrupt_parent=True,
    )
    assert isinstance(repeated, RunSubmission)
    assert repeated.created is False
    assert repeated.run.run_id == replacement.run.run_id

    await wait_until(lambda: replacement.run.run_id in manager._contexts)
    replacement_task = manager._contexts[replacement.run.run_id].task
    assert replacement_task is not None
    await replacement_task
    replacement_stored = await store.get_run(replacement.run.run_id)
    assert replacement_stored is not None
    assert replacement_stored.state == "COMPLETED"
    assert adapter.stream_calls == [selected.session_id, selected.session_id]
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_durable_followup_events_are_published_to_live_run_replay(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-live-followup",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: parent.run.run_id in manager._contexts)
    context = manager._contexts[parent.run.run_id]
    await wait_until(lambda: context.seq > 0)

    followup = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-live-followup",
        payload_digest="b" * 64,
        prompt="please correct that",
        attachment_file_ids=(),
        redirect_eligible=True,
        admission_generation=11,
    )
    assert isinstance(followup, StoredRedirect)
    applying = await store.mark_redirect_boundary_ready(
        followup.redirect_id,
        updated_at=datetime.now(timezone.utc),
    )

    assert all(
        json.loads(chunk)["event_type"]
        not in {"followup.received", "followup.applying"}
        for _, chunk in context.replay
    )
    await manager.publish_durable_followup_events(parent.run.run_id)

    published = [json.loads(chunk) for _, chunk in context.replay]
    assert [
        event["event_type"]
        for event in published
        if event["event_type"].startswith("followup.")
    ] == ["followup.received", "followup.applying"]
    assert context.seq == applying.accepted_event_seq

    adapter.release_by_session[selected.session_id].set()
    task = context.task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_parent_terminal_uses_lifecycle_and_publishes_followup_terminal(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    lifecycle = TerminalFailingRedirectLifecycle(store)
    manager.set_redirect_lifecycle(lifecycle)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-terminal-publish",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: parent.run.run_id in manager._contexts)
    context = manager._contexts[parent.run.run_id]
    await wait_until(lambda: selected.session_id in adapter.release_by_session)
    followup = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-terminal-followup",
        payload_digest="b" * 64,
        prompt="please continue",
        redirect_eligible=True,
        admission_generation=11,
    )
    assert isinstance(followup, StoredRedirect)

    streamed: list[dict[str, object]] = []

    async def collect() -> None:
        async for chunk in manager.subscribe(parent.run.run_id, after_seq=0):
            streamed.append(json.loads(chunk))

    subscriber = asyncio.create_task(collect())
    await asyncio.sleep(0)
    adapter.release_by_session[selected.session_id].set()
    assert context.task is not None
    await context.task
    await subscriber

    event_types = [
        str(item.get("event_type"))
        for item in streamed
        if item.get("type") == "run.event"
    ]
    assert lifecycle.terminal_prepared == [parent.run.run_id]
    assert [
        event_type
        for event_type in event_types
        if event_type.startswith("followup.")
    ] == [
        "followup.applying",
        "followup.failed",
    ]
    assert event_types.index("chat.done") < event_types.index("followup.failed")

    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_redirect_fence_does_not_skip_unpublished_ledger_sequence(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-fence-sequence",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: parent.run.run_id in manager._contexts)
    context = manager._contexts[parent.run.run_id]
    await wait_until(lambda: context.seq > 0)
    before_followup_seq = context.seq
    followup = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-unpublished-before-fence",
        payload_digest="b" * 64,
        prompt="do not skip this receipt",
        redirect_eligible=True,
        admission_generation=11,
    )
    assert isinstance(followup, StoredRedirect)
    assert followup.pending_event_seq > before_followup_seq
    recorded_sequences: list[int] = []

    async def recorder(pre_fence_event_seq: int, _token_digest: str):
        recorded_sequences.append(pre_fence_event_seq)
        return None

    await manager.begin_redirect_event_fence(
        parent_run_id=parent.run.run_id,
        dispatch_group_id="group-unpublished-sequence",
        recorder=recorder,
    )

    assert context.seq == before_followup_seq
    assert recorded_sequences == [before_followup_seq]
    context.redirect_fence_group_id = None
    await manager.publish_durable_followup_events(parent.run.run_id)
    assert context.seq == followup.pending_event_seq

    adapter.release_by_session[selected.session_id].set()
    task = context.task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_expected_followup_after_parent_terminal_becomes_continuation(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-terminal-race",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: selected.session_id in adapter.release_by_session)
    adapter.release_by_session[selected.session_id].set()
    parent_context = manager._contexts[parent.run.run_id]
    parent_task = parent_context.task
    assert parent_task is not None
    await parent_task

    followup = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-terminal-race-followup",
        payload_digest="b" * 64,
        prompt="this was sent while the parent still looked active",
        attachment_file_ids=(),
        redirect_eligible=True,
        expected_parent_run_id=parent.run.run_id,
        admission_generation=11,
    )

    assert isinstance(followup, StoredRedirect)
    assert followup.parent_run_id == parent.run.run_id
    assert followup.state == "PROMOTED_TO_RUN"
    assert followup.promoted_run_id == followup.promotion_reserved_run_id
    continuation = await store.get_run(followup.promoted_run_id)
    assert continuation is not None
    assert continuation.source_detail is not None
    assert continuation.source_detail["followup_parent_run_id"] == parent.run.run_id

    second = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-terminal-race-followup-2",
        payload_digest="c" * 64,
        prompt="and this belongs to the same continuation",
        attachment_file_ids=(),
        redirect_eligible=True,
        expected_parent_run_id=parent.run.run_id,
        admission_generation=11,
    )
    assert isinstance(second, StoredRedirect)
    assert second.state == "PROMOTED_TO_RUN"
    assert second.promoted_run_id == followup.promoted_run_id
    merged = await store.get_run(continuation.run_id)
    assert merged is not None
    assert merged.source_detail is not None
    assert merged.source_detail["followup_client_message_ids"] == [
        "message-terminal-race-followup",
        "message-terminal-race-followup-2",
    ]
    assert merged.prompt == (
        "this was sent while the parent still looked active\n\n"
        "[Additional user correction]\n"
        "and this belongs to the same continuation"
    )

    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_followup_admission_wins_parent_terminal_store_race(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-store-race",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: parent.run.run_id in manager._contexts)
    context = manager._contexts[parent.run.run_id]
    await wait_until(lambda: context.seq > 0)
    original_admit = store.admit_redirect
    raced = False

    async def admit_after_parent_terminal(intent):
        nonlocal raced
        if not raced:
            raced = True
            await store.transition_run(
                parent.run.run_id,
                "COMPLETED",
                terminal_at=datetime.now(timezone.utc),
            )
        return await original_admit(intent)

    store.admit_redirect = admit_after_parent_terminal  # type: ignore[method-assign]
    followup = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-followup-store-race",
        payload_digest="b" * 64,
        prompt="preserve my Follow-up identity",
        redirect_eligible=True,
        expected_parent_run_id=parent.run.run_id,
        admission_generation=11,
    )
    store.admit_redirect = original_admit  # type: ignore[method-assign]

    assert isinstance(followup, StoredRedirect)
    assert followup.parent_run_id == parent.run.run_id
    assert followup.state == "PROMOTED_TO_RUN"
    bound_run = await store.get_run_by_message(
        session_id=selected.session_id,
        client_message_id="message-followup-store-race",
    )
    assert bound_run is not None
    assert bound_run.run_id == followup.promoted_run_id

    adapter.release_by_session[selected.session_id].set()
    task = context.task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_late_followup_never_creates_second_started_continuation(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-one-continuation",
        payload_digest="a" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: selected.session_id in adapter.release_by_session)
    adapter.release_by_session[selected.session_id].set()
    parent_context = manager._contexts[parent.run.run_id]
    assert parent_context.task is not None
    await parent_context.task
    adapter.release_by_session[selected.session_id] = asyncio.Event()

    first = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-first-late-followup",
        payload_digest="b" * 64,
        prompt="first late follow-up",
        redirect_eligible=True,
        expected_parent_run_id=parent.run.run_id,
        admission_generation=11,
    )
    assert isinstance(first, StoredRedirect)
    assert first.promoted_run_id is not None
    await asyncio.sleep(0.11)
    await wait_until(
        lambda: first.promoted_run_id in manager._contexts,
        attempts=1000,
    )

    late = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-too-late-for-same-continuation",
        payload_digest="c" * 64,
        prompt="must not create a second continuation",
        redirect_eligible=True,
        expected_parent_run_id=parent.run.run_id,
        admission_generation=11,
    )
    assert isinstance(late, StoredRedirect)
    assert late.parent_run_id == first.promoted_run_id
    assert late.promoted_run_id is None

    stored_late = await store.get_redirect_by_message(
        session_id=selected.session_id,
        client_message_id="message-too-late-for-same-continuation",
    )
    assert stored_late is not None
    assert stored_late.state == "WAITING_AGENT_READY"

    adapter.release_by_session[selected.session_id].set()
    continuation_context = manager._contexts[first.promoted_run_id]
    assert continuation_context.task is not None
    await continuation_context.task
    stored_late = await store.get_redirect_by_message(
        session_id=selected.session_id,
        client_message_id="message-too-late-for-same-continuation",
    )
    assert stored_late is not None
    assert stored_late.state == "SUBMISSION_UNCERTAIN"
    assert stored_late.promoted_run_id is None
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_manager_close_does_not_cancel_durable_followups(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    lifecycle = RecordingRedirectLifecycle()
    manager.set_redirect_lifecycle(lifecycle)
    selected = identity(9)
    await manager.submit(
        identity=selected,
        client_message_id="message-parent-close",
        payload_digest="9" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(
        lambda: selected.session_id in adapter.release_by_session
    )

    await manager.close()

    assert lifecycle.canceled == []
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_interactive_attachment_is_admitted_as_durable_followup(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(2)
    parent = await manager.submit(
        identity=selected,
        client_message_id="message-parent-0002",
        payload_digest="d" * 64,
        prompt="start",
        source="web",
    )
    await wait_until(lambda: parent.run.run_id in manager._contexts)

    followup = await manager.submit_interactive(
        identity=selected,
        client_message_id="message-attachment-0002",
        payload_digest="e" * 64,
        prompt="see attachment",
        attachment_file_ids=(91,),
        redirect_eligible=True,
        admission_generation=11,
    )
    assert isinstance(followup, StoredRedirect)
    assert followup.state == "WAITING_AGENT_READY"
    assert followup.promotion_run_intent["source_detail"] == {
        "attachment_file_ids": [91]
    }

    await wait_until(lambda: selected.session_id in adapter.release_by_session)
    adapter.release_by_session[selected.session_id].set()
    parent_task = manager._contexts[parent.run.run_id].task
    assert parent_task is not None
    await parent_task
    await wait_until(
        lambda: followup.promotion_reserved_run_id not in manager._contexts
    )
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "delete"])
async def test_skill_manage_observation_is_persisted_as_safe_projection(
    tmp_path: Path,
    action: str,
) -> None:
    store, sessions, capacity, _adapter, manager = await build_manager(
        tmp_path
    )
    selected = identity(1)
    now = datetime.now(timezone.utc)
    stored = await store.persist_dispatch_intent(
        DispatchIntent(
            run_id="run-skill-00000001",
            session_id=selected.session_id,
            client_message_id="message-skill-00000001",
            payload_digest="a" * 64,
            config_revision=selected.config_revision,
            source="web",
            accepted_at=now,
            user_id=9,
            org_id=7,
            profile_id=selected.profile_id,
            model_id=selected.model_id,
            prompt="must-not-enter-skill-projection",
        )
    )
    lease = await capacity.try_acquire()
    context = _RunContext(
        run=stored,
        identity=selected,
        prompt="top-secret prompt",
        audit_prompt="top-secret audit prompt",
        capacity_lease=lease,
        last_snapshot_at=manager.clock(),
    )
    observation = ObservedSkillManageCompletion(
        tool_call_id="call-skill-1",
        action=action,  # type: ignore[arg-type]
        skill_name="demo-skill",
        duration_ms=125,
        observed_at=now,
    )

    await manager._record_event(
        context,
        PlatformStreamEvent(
            event="chat.tool.event",
            data={
                "name": "skill_manage",
                "status": "completed",
                "callId": "call-skill-1",
                "input": '{"action":"create","name":"demo-skill"}',
                "result": '{"success":true}',
            },
            internal=observation,
        ),
    )

    projections = await store.load_projection_batch(max_items=10)
    skill_projections = [
        item
        for item in projections
        if item.projection_type == "skill.manage.observed"
    ]
    assert len(skill_projections) == 1
    projected = skill_projections[0]
    assert projected.projection_id == (
        "skill-manage:"
        + hashlib.sha256(
            f"{stored.run_id}:{context.seq}".encode("utf-8")
        ).hexdigest()
    )
    assert projected.payload == {
        "run_id": stored.run_id,
        "session_id": stored.session_id,
        "profile_id": stored.profile_id,
        "event_seq": context.seq,
        "action": action,
        "skill_name": "demo-skill",
        "tool_call_id": "call-skill-1",
        "duration_ms": 125,
        "occurred_at": now.isoformat(),
        "config_revision": stored.config_revision,
    }
    assert "top-secret" not in str(projected.payload)

    await lease.release()
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_realtime_deltas_are_published_immediately_and_in_order(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(
        tmp_path
    )
    subscriber = _Subscriber()
    context = SimpleNamespace(
        publish_lock=asyncio.Lock(),
        subscriber_by_id={1: subscriber},
    )

    events = [
        b'{"type":"run.event","seq":1}',
        b'{"type":"run.event","seq":2}',
        b'{"type":"run.event","seq":3}',
    ]
    for event in events:
        await manager._publish(
            context,
            event,
            event_type="chat.message.delta",
        )

    received = [await subscriber.queue.get() for _ in events]
    assert received == events
    assert all(b'"type":"run.event_batch"' not in event for event in received)
    assert manager.realtime_snapshot()["slow_client_disconnects"] == 0

    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_first_realtime_publish_yields_to_ready_subscriber(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(
        tmp_path
    )
    subscriber = _Subscriber()
    context = SimpleNamespace(
        publish_lock=asyncio.Lock(),
        subscriber_by_id={1: subscriber},
    )
    received = asyncio.Event()

    async def consume() -> None:
        await subscriber.queue.get()
        received.set()

    consumer = asyncio.create_task(consume())
    await manager._publish(
        context,
        b'{"type":"run.event","seq":1}',
        event_type="chat.message.delta",
    )

    assert received.is_set()
    await consumer
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_each_realtime_publish_yields_to_ready_subscriber(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(
        tmp_path
    )
    subscriber = _Subscriber()
    context = SimpleNamespace(
        publish_lock=asyncio.Lock(),
        subscriber_by_id={1: subscriber},
    )
    received: list[bytes] = []

    async def consume() -> None:
        for _ in range(3):
            chunk = await subscriber.queue.get()
            assert chunk is not None
            received.append(chunk)

    consumer = asyncio.create_task(consume())
    for seq in range(1, 4):
        chunk = f'{{"type":"run.event","seq":{seq}}}'.encode()
        await manager._publish(
            context,
            chunk,
            event_type="chat.reasoning.delta",
        )
        assert received == [
            f'{{"type":"run.event","seq":{item}}}'.encode()
            for item in range(1, seq + 1)
        ]

    await consumer
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_slow_subscriber_is_disconnected_when_its_queue_is_full(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(
        tmp_path
    )
    subscriber = _Subscriber()
    context = SimpleNamespace(
        publish_lock=asyncio.Lock(),
        subscriber_by_id={1: subscriber},
    )
    subscriber.queued_bytes = PER_RUN_REALTIME_LIMIT - 1

    await manager._publish(
        context,
        b"{}",
        event_type="chat.message.delta",
    )

    assert subscriber.closed
    assert await subscriber.queue.get() is None
    assert manager.realtime_snapshot()["slow_client_disconnects"] == 1
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_prepare_singleflight_and_identity_change(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []
    closed: list[str] = []

    async def prepare(item: PrepareIdentity) -> None:
        calls.append(item.digest)
        entered.set()
        await release.wait()

    async def close(item: PrepareIdentity) -> None:
        closed.append(item.digest)

    sessions = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=1,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    first = identity(1)
    tasks = [asyncio.create_task(sessions.prepare(first)) for _ in range(10)]
    await entered.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(calls) == 1
    assert len({id(item) for item in results}) == 1

    changed = replace(first, config_revision="revision-19")
    await sessions.prepare(changed)
    assert closed == [first.digest]
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_prepare_waits_for_stale_close_before_starting_new_identity(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    events: list[str] = []
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def prepare(item: PrepareIdentity) -> None:
        events.append(f"prepare:{item.config_revision}")

    async def close(item: PrepareIdentity) -> None:
        events.append(f"close-start:{item.config_revision}")
        close_started.set()
        await allow_close.wait()
        events.append(f"close-done:{item.config_revision}")

    sessions = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=1,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    first = identity(1)
    changed = replace(first, config_revision="revision-changed")
    await sessions.prepare(first)

    changed_prepare = asyncio.create_task(sessions.prepare(changed))
    await close_started.wait()
    await asyncio.sleep(0)

    assert events == [
        f"prepare:{first.config_revision}",
        f"close-start:{first.config_revision}",
    ]
    allow_close.set()
    await changed_prepare

    assert events == [
        f"prepare:{first.config_revision}",
        f"close-start:{first.config_revision}",
        f"close-done:{first.config_revision}",
        f"prepare:{changed.config_revision}",
    ]
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_different_identities_for_one_session_prepare_serially(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    preparing = 0
    peak_preparing = 0
    calls: list[str] = []
    closed: list[str] = []

    async def prepare(item: PrepareIdentity) -> None:
        nonlocal preparing, peak_preparing
        calls.append(item.digest)
        preparing += 1
        peak_preparing = max(peak_preparing, preparing)
        try:
            if len(calls) == 1:
                first_entered.set()
                await release_first.wait()
        finally:
            preparing -= 1

    async def close(item: PrepareIdentity) -> None:
        closed.append(item.digest)

    sessions = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=1,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    first = identity(1)
    changed = replace(first, model_id="model-2")
    first_task = asyncio.create_task(sessions.prepare(first))
    await first_entered.wait()
    changed_task = asyncio.create_task(sessions.prepare(changed))
    await asyncio.sleep(0)
    assert calls == [first.digest]

    release_first.set()
    first_result, changed_result = await asyncio.gather(
        first_task,
        changed_task,
    )
    assert first_result.identity == first
    assert changed_result.identity == changed
    assert calls == [first.digest, changed.digest]
    assert closed == [first.digest]
    assert peak_preparing == 1

    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_inflight_prepares_reserve_the_resident_session_limit(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    entered_count = 0

    async def prepare(_: PrepareIdentity) -> None:
        nonlocal entered_count
        entered_count += 1
        if entered_count == 2:
            entered.set()
        await release.wait()

    async def close(_: PrepareIdentity) -> None:
        return None

    sessions = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    first = asyncio.create_task(sessions.prepare(identity(1)))
    second = asyncio.create_task(sessions.prepare(identity(2)))
    await entered.wait()

    with pytest.raises(
        SessionCapacityBusy,
        match="RUNTIME_SESSION_CAPACITY_BUSY",
    ):
        await sessions.prepare(identity(3))
    assert entered_count == 2

    release.set()
    await asyncio.gather(first, second)
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_recent_session_restore_is_background_and_revision_scoped(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    restored: list[str] = []

    async def prepare(item: PrepareIdentity) -> None:
        restored.append(item.session_id)

    async def close(_: PrepareIdentity) -> None:
        return None

    original = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=1,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    await original.prepare(identity(1))
    await original.close()
    restored.clear()

    restarted = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=1,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    restarted.start_background_restore(config_revision="revision-18")
    assert restarted.resident_count == 0
    assert restarted._restore_task is not None
    await restarted._restore_task
    assert restored == [identity(1).session_id]
    assert restarted.resident_count == 1
    await restarted.close()
    await store.close()


@pytest.mark.asyncio
async def test_capacity_busy_happens_before_durable_run(tmp_path: Path) -> None:
    store, sessions, capacity, adapter, manager = await build_manager(tmp_path, limit=2)
    submissions = []
    for index in (1, 2):
        prompt = f"prompt-{index}"
        submissions.append(
            await manager.submit(
                identity=identity(index),
                client_message_id=f"message-{index:08d}",
                payload_digest=hashlib.sha256(prompt.encode()).hexdigest(),
                prompt=prompt,
                source="application_wecom",
            )
        )
    queued = await manager.submit(
        identity=identity(3),
        client_message_id="message-00000003",
        payload_digest="3" * 64,
        prompt="third",
        source="application_wecom",
    )
    queued_run = await store.get_run_by_message(
        session_id=identity(3).session_id,
        client_message_id="message-00000003",
    )
    assert queued_run is not None
    assert queued_run.state == "QUEUED"
    assert capacity.inflight == 2

    first = submissions[0]
    adapter.release_by_session.setdefault(
        first.run.session_id,
        asyncio.Event(),
    ).set()
    first_task = manager._contexts[first.run.run_id].task
    assert first_task is not None
    await first_task
    await asyncio.gather(*tuple(manager._drain_tasks))
    assert queued.run.run_id in manager._contexts
    assert capacity.inflight == 2

    for item in submissions[1:] + [queued]:
        adapter.release_by_session.setdefault(
            item.run.session_id,
            asyncio.Event(),
        ).set()
    await asyncio.gather(
        *(manager._contexts[item.run.run_id].task for item in submissions[1:] + [queued])
    )
    assert capacity.inflight == 0
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_personal_capacity_limit_is_pod_local_and_uses_personal_error(
    tmp_path: Path,
) -> None:
    first = await build_manager(
        tmp_path / "first",
        limit=10,
        capacity_busy_code="PERSONAL_ACTIVE_SESSION_LIMIT",
    )
    second = await build_manager(
        tmp_path / "second",
        limit=10,
        capacity_busy_code="PERSONAL_ACTIVE_SESSION_LIMIT",
    )
    store_a, sessions_a, capacity_a, adapter_a, manager_a = first
    store_b, sessions_b, capacity_b, adapter_b, manager_b = second
    submissions_a = []
    submissions_b = []
    for index in range(1, 11):
        prompt = f"prompt-{index}"
        submissions_a.append(
            await manager_a.submit(
                identity=identity(index),
                client_message_id=f"message-{index:08d}",
                payload_digest=hashlib.sha256(prompt.encode()).hexdigest(),
                prompt=prompt,
                source="web",
            )
        )
        submissions_b.append(
            await manager_b.submit(
                identity=identity(index),
                client_message_id=f"other-message-{index:08d}",
                payload_digest=hashlib.sha256(
                    f"other-{prompt}".encode()
                ).hexdigest(),
                prompt=f"other-{prompt}",
                source="web",
            )
        )

    queued = await manager_a.submit(
        identity=identity(11),
        client_message_id="message-00000011",
        payload_digest="b" * 64,
        prompt="eleventh",
        source="web",
    )

    queued_run = await store_a.get_run_by_message(
        session_id=identity(11).session_id,
        client_message_id="message-00000011",
    )
    assert queued_run is not None
    assert queued_run.state == "QUEUED"
    assert capacity_a.inflight == 10
    assert capacity_b.inflight == 10

    first_a = submissions_a[0]
    adapter_a.release_by_session.setdefault(
        first_a.run.session_id,
        asyncio.Event(),
    ).set()
    first_a_task = manager_a._contexts[first_a.run.run_id].task
    assert first_a_task is not None
    await first_a_task
    await asyncio.gather(*tuple(manager_a._drain_tasks))
    assert queued.run.run_id in manager_a._contexts
    assert capacity_a.inflight == 10
    assert capacity_b.inflight == 10

    for item in submissions_a[1:] + [queued]:
        adapter_a.release_by_session.setdefault(
            item.run.session_id,
            asyncio.Event(),
        ).set()
    for item in submissions_b:
        adapter_b.release_by_session.setdefault(
            item.run.session_id,
            asyncio.Event(),
        ).set()
    await asyncio.gather(
        *(
            manager_a._contexts[item.run.run_id].task
            for item in submissions_a[1:] + [queued]
        ),
        *(
            manager_b._contexts[item.run.run_id].task
            for item in submissions_b
        ),
    )
    await manager_a.close()
    await manager_b.close()
    await sessions_a.close()
    await sessions_b.close()
    await store_a.close()
    await store_b.close()


@pytest.mark.asyncio
async def test_ten_personal_sessions_keep_identity_events_and_cancel_isolated(
    tmp_path: Path,
) -> None:
    store, sessions, capacity, _adapter, manager = await build_manager(
        tmp_path,
        limit=10,
        capacity_busy_code="PERSONAL_ACTIVE_SESSION_LIMIT",
    )
    adapter = TenSessionIsolationAdapter()
    model_router = FakeModelRouter()
    manager.adapter = adapter
    manager.model_router = model_router
    submissions = []
    expected: dict[str, tuple[PrepareIdentity, str, str]] = {}
    for index in range(1, 11):
        selected = replace(
            identity(index),
            profile_id=f"profile-{index}",
            model_id=str(100 + index),
            user_id=700 + index,
            org_id=3,
        )
        prompt = f"file-{index}.txt::prompt-{index}"
        submission = await manager.submit(
            identity=selected,
            client_message_id=f"message-isolation-{index}",
            payload_digest=hashlib.sha256(prompt.encode()).hexdigest(),
            prompt=prompt,
            source="web",
        )
        submissions.append(submission)
        expected[selected.session_id] = (
            selected,
            prompt,
            submission.run.run_id,
        )

    await wait_until(
        lambda: len(adapter.seen) == 10 and len(adapter.release) == 10
    )
    assert adapter.seen == expected
    assert set(model_router.activated) == {
        (selected.session_id, run_id)
        for selected, _prompt, run_id in expected.values()
    }

    canceled = submissions[4]
    canceled_identity, _prompt, canceled_run_id = expected[
        canceled.run.session_id
    ]
    assert await manager.cancel(canceled_run_id) == "accepted"
    assert adapter.cancel_calls == [
        (
            canceled_identity.session_id,
            f"live-{canceled_identity.session_id}-{canceled_run_id}",
        )
    ]
    for session_id, release in adapter.release.items():
        if session_id != canceled_identity.session_id:
            release.set()
    await asyncio.gather(
        *(manager._contexts[item.run.run_id].task for item in submissions)
    )

    for item in submissions:
        selected, prompt, run_id = expected[item.run.session_id]
        snapshot = await store.load_run_snapshot(run_id)
        assert snapshot is not None
        expected_answer = "|".join(
            (
                selected.runtime_id,
                selected.session_id,
                selected.profile_id,
                selected.model_id,
                prompt,
            )
        )
        assert snapshot.answer == expected_answer
        assert snapshot.run.runtime_provider_run_id == (
            f"live-{selected.session_id}-{run_id}"
        )
        assert snapshot.run.state == (
            "CANCELED"
            if selected.session_id == canceled_identity.session_id
            else "COMPLETED"
        )
        assert all(
                event.payload.get("session_id") in {None, selected.session_id}
            for event in snapshot.events
        )
    assert capacity.inflight == 0
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_same_session_is_reserved_before_prepare_or_persistence(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    prepare_entered = asyncio.Event()
    allow_prepare = asyncio.Event()
    prepare_calls = 0

    async def prepare(_: PrepareIdentity) -> None:
        nonlocal prepare_calls
        prepare_calls += 1
        prepare_entered.set()
        await allow_prepare.wait()

    async def close(_: PrepareIdentity) -> None:
        return None

    sessions = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=10,
        max_resident_sessions=14,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    capacity = CapacityGate(10)
    adapter = FakeAdapter()
    manager = RunManager(store, sessions, capacity, adapter)
    selected = identity(1)
    first_task = asyncio.create_task(
        manager.submit(
            identity=selected,
            client_message_id="message-00000001",
            payload_digest="a" * 64,
            prompt="first",
            source="web",
        )
    )
    await prepare_entered.wait()

    second = await asyncio.wait_for(
        manager.submit(
            identity=selected,
            client_message_id="message-00000002",
            payload_digest="b" * 64,
            prompt="second",
            source="web",
        ),
        timeout=0.1,
    )

    assert prepare_calls == 1
    queued = await store.get_run_by_message(
        session_id=selected.session_id,
        client_message_id="message-00000002",
    )
    assert queued is not None
    assert queued.run_id == second.run.run_id
    assert queued.state == "QUEUED"
    allow_prepare.set()
    submission = await first_task
    adapter.release_by_session.setdefault(selected.session_id, asyncio.Event()).set()
    task = manager._contexts[submission.run.run_id].task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_two_concurrent_scheduled_tasks_share_one_session_fifo(
    tmp_path: Path,
) -> None:
    store, sessions, capacity, adapter, manager = await build_manager(
        tmp_path,
        limit=2,
    )
    selected = identity(1)

    async def submit(index: int):
        return await manager.submit(
            identity=selected,
            run_id=f"scheduled-run-{index}",
            client_message_id=f"scheduled-message-{index}",
            payload_digest=f"{index:064x}",
            prompt=f"scheduled prompt {index}",
            source="scheduled_task",
            source_detail={
                "type": "scheduled",
                "scheduled_task_id": index,
                "scheduled_task_name": f"task-{index}",
                "trigger_kind": "scheduled",
                "task_available": True,
            },
            scheduled_task_id=index,
            scheduled_occurrence_key=f"scheduled:{index}:same-fire",
        )

    first, second = await asyncio.gather(submit(7), submit(8))
    stored = [
        await store.get_run(first.run.run_id),
        await store.get_run(second.run.run_id),
    ]
    assert sum(run is not None and run.state == "QUEUED" for run in stored) == 1
    assert len(manager._contexts) == 1
    assert capacity.inflight == 1

    await wait_until(
        lambda: selected.session_id in adapter.release_by_session
    )
    adapter.release_by_session[selected.session_id].set()
    for _ in range(200):
        refreshed = [
            await store.get_run(first.run.run_id),
            await store.get_run(second.run.run_id),
        ]
        if all(run is not None and run.state == "COMPLETED" for run in refreshed):
            break
        await asyncio.sleep(0)
    assert all(run is not None and run.state == "COMPLETED" for run in refreshed)
    assert adapter.stream_calls == [selected.session_id, selected.session_id]
    await wait_until(lambda: capacity.inflight == 0)
    assert capacity.inflight == 0

    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_fifo_closes_terminal_adapter_stream_before_dispatching_next_run(
    tmp_path: Path,
) -> None:
    store, sessions, capacity, _adapter, manager = await build_manager(
        tmp_path,
        limit=2,
    )
    adapter = TerminalLeaseAdapter()
    manager.adapter = adapter
    selected = identity(1)

    first = await manager.submit(
        identity=selected,
        run_id="web-run-first",
        client_message_id="web-message-first",
        payload_digest="a" * 64,
        prompt="first",
        source="web",
    )
    await adapter.first_started.wait()
    second = await manager.submit(
        identity=selected,
        run_id="web-run-second",
        client_message_id="web-message-second",
        payload_digest="b" * 64,
        prompt="second",
        source="web",
    )
    assert first.run.enqueue_seq < second.run.enqueue_seq
    assert second.run.state == "QUEUED"

    adapter.release_first.set()
    for _ in range(200):
        stored = [
            await store.get_run(first.run.run_id),
            await store.get_run(second.run.run_id),
        ]
        if all(run is not None and run.state == "COMPLETED" for run in stored):
            break
        await asyncio.sleep(0)

    assert [run.state if run is not None else None for run in stored] == [
        "COMPLETED",
        "COMPLETED",
    ]
    assert adapter.stream_calls == [first.run.run_id, second.run.run_id]
    for run in stored:
        assert run is not None
        snapshot = await store.load_run_snapshot(run.run_id)
        assert snapshot is not None
        assert [event.event_type for event in snapshot.events].count(
            "chat.run.started"
        ) == 1
        assert [event.event_type for event in snapshot.events].count(
            "chat.done"
        ) == 1
    await wait_until(lambda: capacity.inflight == 0)

    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_permanent_head_admission_failure_drains_next_session_candidate(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    first_prepare_entered = asyncio.Event()
    release_first_prepare = asyncio.Event()
    prepare_calls = 0

    async def prepare(_: PrepareIdentity) -> None:
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            first_prepare_entered.set()
            await release_first_prepare.wait()
            raise RuntimeError("first candidate cannot be prepared")

    async def close(_: PrepareIdentity) -> None:
        return None

    sessions = SessionManager(
        store,
        runtime_id="application-runtime-1",
        max_inflight_runs=10,
        max_resident_sessions=14,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    capacity = CapacityGate(10)
    adapter = FakeAdapter()
    manager = RunManager(store, sessions, capacity, adapter)
    selected = identity(1)

    first_task = asyncio.create_task(
        manager.submit(
            identity=selected,
            client_message_id="message-failing-head",
            payload_digest="a" * 64,
            prompt="first",
            source="web",
        )
    )
    await first_prepare_entered.wait()
    second = await manager.submit(
        identity=selected,
        client_message_id="message-after-failure",
        payload_digest="b" * 64,
        prompt="second",
        source="scheduled_task",
        scheduled_task_id=7,
        scheduled_occurrence_key="scheduled:7:after-failure",
    )
    assert second.run.state == "QUEUED"

    release_first_prepare.set()
    first = await first_task
    assert first.run.state == "FAILED"
    failed_snapshot = await store.load_run_snapshot(first.run.run_id)
    assert failed_snapshot is not None
    assert [event.event_type for event in failed_snapshot.events] == [
        "chat.error",
        "chat.done",
    ]
    assert failed_snapshot.events[0].payload == {
        "admission_stage": "session_prepare",
        "code": "RUNTIME_ADMISSION_SESSION_PREPARE_FAILED",
        "message": "Run admission failed during session_prepare",
    }
    terminal = next(
        item
        for item in await store.load_projection_batch(max_items=20)
        if item.projection_id == f"{first.run.run_id}:terminal"
    )
    assert terminal.payload["terminal_seq"] == 2
    assert terminal.payload["admission_stage"] == "session_prepare"
    assert (
        terminal.payload["error_code"]
        == "RUNTIME_ADMISSION_SESSION_PREPARE_FAILED"
    )
    await wait_until(lambda: second.run.run_id in manager._contexts)
    assert prepare_calls == 2

    await wait_until(
        lambda: selected.session_id in adapter.release_by_session
    )
    adapter.release_by_session[selected.session_id].set()
    second_task = manager._contexts[second.run.run_id].task
    assert second_task is not None
    await second_task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_restore_blocks_uncertain_and_acknowledged_runs_in_recovery(
    tmp_path: Path,
) -> None:
    store, sessions, capacity, _adapter, manager = await build_manager(
        tmp_path,
        limit=2,
    )
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    for index in (1, 2):
        selected = identity(index)
        await store.persist_dispatch_intent(
            DispatchIntent(
                run_id=f"run-{index:08d}",
                session_id=selected.session_id,
                client_message_id=f"message-{index:08d}",
                payload_digest=f"{index:064x}",
                config_revision=selected.config_revision,
                source="web",
                accepted_at=now,
            )
        )
    await store.transition_run(
        "run-00000002",
        "ACKNOWLEDGED",
        acknowledged_at=now,
    )

    await manager.restore()

    unacknowledged = await store.get_run("run-00000001")
    acknowledged = await store.get_run("run-00000002")
    assert unacknowledged is not None
    assert unacknowledged.state == "RECOVERING"
    assert acknowledged is not None
    assert acknowledged.state == "RECOVERING"
    assert capacity.inflight == 2
    assert {
        item.run_id for item in await store.load_unfinished_runs()
    } == {"run-00000001", "run-00000002"}

    terminal_projections = [
        item
        for item in await store.load_projection_batch(max_items=10)
        if item.projection_type == "run.terminal"
    ]
    assert terminal_projections == []

    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_cancel_recovered_run_releases_capacity_and_drains_queued_followup(
    tmp_path: Path,
) -> None:
    store, sessions, capacity, adapter, manager = await build_manager(
        tmp_path,
        limit=1,
    )
    selected = identity(1)
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    await store.persist_dispatch_intent(
        DispatchIntent(
            run_id="recovered-parent",
            session_id=selected.session_id,
            client_message_id="recovered-parent-message",
            payload_digest="a" * 64,
            config_revision=selected.config_revision,
            source="web",
            accepted_at=now,
        )
    )
    await store.transition_run(
        "recovered-parent",
        "ACKNOWLEDGED",
        acknowledged_at=now,
    )
    queued = await store.enqueue_run(
        RunEnqueueIntent(
            run_id="queued-followup",
            session_id=selected.session_id,
            client_message_id="queued-followup-message",
            payload_digest="b" * 64,
            config_revision=selected.config_revision,
            source="web",
            enqueued_at=now,
            prompt="continue after recovered parent",
            audit_prompt="continue after recovered parent",
            identity=asdict(selected),
        )
    )

    await manager.restore()

    assert capacity.inflight == 1
    assert queued.run_id not in manager._contexts

    assert await manager.cancel("recovered-parent") == "accepted"

    recovered = await store.get_run("recovered-parent")
    assert recovered is not None
    assert recovered.state == "CANCELED"
    assert queued.run_id in manager._contexts
    assert capacity.inflight == 1
    terminal_projections = [
        item
        for item in await store.load_projection_batch(max_items=20)
        if item.projection_type == "run.terminal"
        and item.payload["run_id"] == "recovered-parent"
    ]
    assert len(terminal_projections) == 1

    adapter.release_by_session.setdefault(
        selected.session_id,
        asyncio.Event(),
    ).set()
    task = manager._contexts[queued.run_id].task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_restore_keeps_queued_run_and_dispatches_it(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(
        tmp_path,
        limit=1,
    )
    selected = identity(1)
    queued = await store.enqueue_run(
        RunEnqueueIntent(
            run_id="queued-after-restart",
            session_id=selected.session_id,
            client_message_id="message-after-restart",
            payload_digest="d" * 64,
            config_revision=selected.config_revision,
            source="scheduled_task",
            enqueued_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
            prompt="scheduled prompt",
            audit_prompt="scheduled prompt",
            identity=asdict(selected),
            source_detail={
                "type": "scheduled",
                "scheduled_task_id": 7,
                "scheduled_task_name": "daily",
                "trigger_kind": "scheduled",
                "task_available": True,
            },
            scheduled_task_id=7,
            scheduled_occurrence_key="scheduled:7:2026-08-14T11:00:00Z",
        )
    )
    assert queued.state == "QUEUED"

    await manager.restore()

    assert queued.run_id in manager._contexts
    restored = await store.get_run(queued.run_id)
    assert restored is not None
    assert restored.state in {"DISPATCH_INTENT_DURABLE", "ACKNOWLEDGED"}
    assert restored.history_message_count_before_run == 0
    adapter.release_by_session.setdefault(selected.session_id, asyncio.Event()).set()
    task = manager._contexts[queued.run_id].task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.parametrize(
    ("failure_stage", "expected_state"),
    [
        ("before_submit", "FAILED"),
        ("before_ack", "FAILED_SUBMISSION_UNCERTAIN"),
        ("after_ack", "FAILED"),
        ("after_first_token", "FAILED"),
    ],
)
@pytest.mark.asyncio
async def test_submission_failure_state_matches_hermes_ack_boundary(
    tmp_path: Path,
    failure_stage: str,
    expected_state: str,
) -> None:
    store, sessions, capacity, _adapter, manager = await build_manager(tmp_path)
    adapter = FailingAdapter(failure_stage)
    manager.adapter = adapter
    model_router = FakeModelRouter()
    manager.model_router = model_router
    selected = identity(1)

    submission = await manager.submit(
        identity=selected,
        client_message_id="message-00000001",
        payload_digest="a" * 64,
        prompt="hello",
        source="web",
    )
    task = manager._contexts[submission.run.run_id].task
    assert task is not None
    adapter.release.set()
    await task

    stored = await store.get_run(submission.run.run_id)
    assert stored is not None
    assert stored.state == expected_state
    assert capacity.inflight == 0
    session_metrics = await sessions.metrics_snapshot()
    assert session_metrics["active"] == 0
    assert model_router.activated == [(selected.session_id, submission.run.run_id)]
    assert model_router.finished == [
        (selected.session_id, submission.run.run_id, False)
    ]

    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_queued_prompt_ack_waits_for_drain_and_stays_idempotent(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(tmp_path)
    selected = identity(1)

    class QueuedPromptAdapter:
        def __init__(self) -> None:
            self.stream_calls = 0
            self.acknowledged = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(
            self,
            *,
            identity: PrepareIdentity,
            run_id: str,
            prompt: str,
            trusted_context=None,
            attachments=(),
            prompt_submit_started_sink=None,
            prompt_ack_sink=None,
            runtime_provider_run_id_sink=None,
        ) -> AsyncIterator[PlatformStreamEvent]:
            del prompt, trusted_context, attachments
            self.stream_calls += 1
            if runtime_provider_run_id_sink is not None:
                await runtime_provider_run_id_sink("live-queued")
            if prompt_submit_started_sink is not None:
                started = prompt_submit_started_sink()
                if started is not None:
                    await started
            if prompt_ack_sink is not None:
                await prompt_ack_sink()
            self.acknowledged.set()
            await self.release.wait()
            yield PlatformStreamEvent(
                "chat.message.delta",
                {"delta": "完成", "source": "agent"},
            )
            yield PlatformStreamEvent("chat.message.completed", {"text": "完成"})
            yield PlatformStreamEvent(
                "chat.done",
                {"run_id": "live-queued", "status": "completed"},
            )

        async def cancel(
            self,
            identity: PrepareIdentity,
            *,
            runtime_provider_run_id: str | None = None,
        ) -> None:
            del identity, runtime_provider_run_id
            self.release.set()

    adapter = QueuedPromptAdapter()
    manager.adapter = adapter
    submission = await manager.submit(
        identity=selected,
        client_message_id="message-queued-0001",
        payload_digest="a" * 64,
        prompt="next turn",
        source="web",
    )
    await asyncio.wait_for(adapter.acknowledged.wait(), timeout=1)

    acknowledged = await store.get_run(submission.run.run_id)
    assert acknowledged is not None
    assert acknowledged.state == "ACKNOWLEDGED"

    repeated = await manager.submit(
        identity=selected,
        client_message_id="message-queued-0001",
        payload_digest="a" * 64,
        prompt="next turn",
        source="web",
    )
    assert repeated.created is False
    assert repeated.run.run_id == submission.run.run_id
    assert adapter.stream_calls == 1

    adapter.release.set()
    task = manager._contexts[submission.run.run_id].task
    assert task is not None
    await task

    snapshot = await store.load_run_snapshot(submission.run.run_id)
    assert snapshot is not None
    assert snapshot.run.state == "COMPLETED"
    assert snapshot.events
    assert all(event.event_type != "chat.error" for event in snapshot.events)
    assert any(
        item.projection_type == "run.terminal"
        for item in await store.load_projection_batch(max_items=50)
    )
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_cancel_after_queued_prompt_ack_reaches_terminal_state(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(tmp_path)
    selected = identity(1)

    class CancelableQueuedAdapter:
        def __init__(self) -> None:
            self.acknowledged = asyncio.Event()
            self.release = asyncio.Event()
            self.cancel_calls: list[tuple[str, str | None]] = []

        async def stream(
            self,
            *,
            identity: PrepareIdentity,
            run_id: str,
            prompt: str,
            trusted_context=None,
            attachments=(),
            prompt_submit_started_sink=None,
            prompt_ack_sink=None,
            runtime_provider_run_id_sink=None,
        ) -> AsyncIterator[PlatformStreamEvent]:
            del run_id, prompt, trusted_context, attachments
            if runtime_provider_run_id_sink is not None:
                await runtime_provider_run_id_sink("live-queued-cancel")
            if prompt_submit_started_sink is not None:
                started = prompt_submit_started_sink()
                if started is not None:
                    await started
            if prompt_ack_sink is not None:
                await prompt_ack_sink()
            self.acknowledged.set()
            await self.release.wait()
            yield PlatformStreamEvent(
                "chat.done",
                {"run_id": "live-queued-cancel", "status": "canceled"},
            )

        async def cancel(
            self,
            identity: PrepareIdentity,
            *,
            runtime_provider_run_id: str | None = None,
        ) -> None:
            self.cancel_calls.append(
                (identity.session_id, runtime_provider_run_id)
            )
            self.release.set()

    adapter = CancelableQueuedAdapter()
    manager.adapter = adapter
    submission = await manager.submit(
        identity=selected,
        client_message_id="message-queued-cancel",
        payload_digest="b" * 64,
        prompt="cancel queued turn",
        source="web",
    )
    await asyncio.wait_for(adapter.acknowledged.wait(), timeout=1)

    assert await manager.cancel(submission.run.run_id) == "accepted"
    assert adapter.cancel_calls == [
        (selected.session_id, "live-queued-cancel")
    ]
    task = manager._contexts[submission.run.run_id].task
    assert task is not None
    await task

    stored = await store.get_run(submission.run.run_id)
    assert stored is not None
    assert stored.state == "CANCELED"
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_cancel_uses_live_dashboard_session_id_after_run_starts(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, adapter, manager = await build_manager(tmp_path)
    selected = identity(1)
    runtime_run_id = "run-cancel-00000001"
    adapter.provider_run_ids[runtime_run_id] = "dashboard-live-123"
    submission = await manager.submit(
        identity=selected,
        client_message_id="message-00000001",
        payload_digest="a" * 64,
        prompt="hello",
        source="web",
        run_id=runtime_run_id,
    )

    context = manager._contexts[submission.run.run_id]
    for _ in range(20):
        if context.seq > 0:
            break
        await asyncio.sleep(0)
    assert context.seq > 0

    assert await manager.cancel(submission.run.run_id) == "accepted"
    assert adapter.cancel_calls == [
        (selected.session_id, "dashboard-live-123"),
    ]

    task = context.task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_cancel_before_prompt_stops_without_submitting_or_interrupting(
    tmp_path: Path,
) -> None:
    store, sessions, capacity, _adapter, manager = await build_manager(tmp_path)
    adapter = ControllableCancelAdapter(
        publish_provider_before_prompt=True,
    )
    manager.adapter = adapter
    submission = await manager.submit(
        identity=identity(1),
        client_message_id="message-pre-prompt-cancel",
        payload_digest="d" * 64,
        prompt="cancel before submit",
        source="web",
    )
    context = manager._contexts[submission.run.run_id]
    await adapter.entered.wait()

    assert await manager.cancel(submission.run.run_id) == "accepted"
    task = context.task
    assert task is not None
    await task

    stored = await store.get_run(submission.run.run_id)
    assert stored is not None
    assert stored.state == "CANCELED"
    assert stored.cancellation_requested_at is not None
    assert stored.runtime_provider_run_id is None
    assert adapter.prompt_started is False
    assert adapter.cancel_calls == []
    assert capacity.inflight == 0
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_cancel_during_ack_wait_uses_early_durable_live_id_once(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(tmp_path)
    adapter = ControllableCancelAdapter(
        publish_provider_before_prompt=True,
    )
    manager.adapter = adapter
    selected = identity(1)
    submission = await manager.submit(
        identity=selected,
        client_message_id="message-ack-wait-cancel",
        payload_digest="e" * 64,
        prompt="cancel during ack",
        source="web",
    )
    context = manager._contexts[submission.run.run_id]
    adapter.allow_prompt.set()
    await wait_until(
        lambda: adapter.prompt_started
        and context.runtime_provider_run_id is not None
    )

    assert await manager.cancel(submission.run.run_id) == "accepted"
    assert await manager.cancel(submission.run.run_id) == "accepted"
    assert adapter.cancel_calls == [
        (selected.session_id, f"live-{selected.session_id}"),
    ]
    task = context.task
    assert task is not None
    await task

    stored = await store.get_run(submission.run.run_id)
    assert stored is not None
    assert stored.state == "CANCELED"
    assert stored.runtime_provider_run_id == f"live-{selected.session_id}"
    assert stored.cancellation_requested_at is not None
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_cancel_intent_waits_for_live_id_then_interrupts_exact_target(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(tmp_path)
    adapter = ControllableCancelAdapter(
        publish_provider_before_prompt=False,
    )
    manager.adapter = adapter
    selected = identity(1)
    submission = await manager.submit(
        identity=selected,
        client_message_id="message-late-live-id",
        payload_digest="f" * 64,
        prompt="late live id",
        source="web",
    )
    context = manager._contexts[submission.run.run_id]
    adapter.allow_prompt.set()
    await wait_until(lambda: adapter.prompt_started)

    assert await manager.cancel(submission.run.run_id) == "accepted"
    assert adapter.cancel_calls == []
    assert adapter.provider_sink is not None
    await adapter.provider_sink("live-late-mapping")
    assert adapter.cancel_calls == [
        (selected.session_id, "live-late-mapping"),
    ]
    task = context.task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_interrupt_failure_is_reported_without_fake_canceled_terminal(
    tmp_path: Path,
) -> None:
    store, sessions, _capacity, _adapter, manager = await build_manager(tmp_path)
    adapter = ControllableCancelAdapter(
        publish_provider_before_prompt=True,
    )
    adapter.cancel_fails = True
    manager.adapter = adapter
    submission = await manager.submit(
        identity=identity(1),
        client_message_id="message-interrupt-failure",
        payload_digest="1" * 64,
        prompt="interrupt failure",
        source="web",
    )
    context = manager._contexts[submission.run.run_id]
    adapter.allow_prompt.set()
    await wait_until(lambda: adapter.prompt_started)

    assert await manager.cancel(submission.run.run_id) == "failed"
    stored = await store.get_run(submission.run.run_id)
    assert stored is not None
    assert stored.state not in {"CANCELED", "COMPLETED", "FAILED"}

    adapter.cancel_fails = False
    assert await manager.cancel(submission.run.run_id) == "accepted"
    task = context.task
    assert task is not None
    await task
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_cancel_preserves_partial_answer_and_does_not_affect_other_session(
    tmp_path: Path,
) -> None:
    store, sessions, capacity, _adapter, manager = await build_manager(
        tmp_path,
        limit=2,
    )
    adapter = MultiSessionCancelAdapter()
    manager.adapter = adapter
    first = await manager.submit(
        identity=identity(1),
        client_message_id="message-session-a",
        payload_digest="2" * 64,
        prompt="session a",
        source="web",
    )
    second = await manager.submit(
        identity=identity(2),
        client_message_id="message-session-b",
        payload_digest="3" * 64,
        prompt="session b",
        source="web",
    )
    await wait_until(
        lambda: all(
            adapter.started.get(item.run.session_id, asyncio.Event()).is_set()
            for item in (first, second)
        )
    )
    first_context = manager._contexts[first.run.run_id]
    second_context = manager._contexts[second.run.run_id]

    assert await manager.cancel(first.run.run_id) == "accepted"
    assert first_context.task is not None
    await first_context.task

    first_snapshot = await store.load_run_snapshot(first.run.run_id)
    assert first_snapshot is not None
    assert first_snapshot.run.state == "CANCELED"
    assert first_snapshot.answer == "部分回答"
    assert second_context.task is not None
    assert second_context.task.done() is False
    assert capacity.inflight == 1
    assert adapter.cancel_calls == [
        (identity(1).session_id, f"live-{identity(1).session_id}"),
    ]
    assert await manager.cancel(first.run.run_id) == "already_terminal"

    adapter.release[identity(2).session_id].set()
    await second_context.task
    second_snapshot = await store.load_run_snapshot(second.run.run_id)
    assert second_snapshot is not None
    assert second_snapshot.run.state == "COMPLETED"
    assert await manager.cancel(second.run.run_id) == "already_terminal"
    unchanged = await store.get_run(second.run.run_id)
    assert unchanged is not None
    assert unchanged.state == "COMPLETED"
    assert capacity.inflight == 0
    await manager.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_idempotency_session_serialization_and_terminal_snapshot(tmp_path: Path) -> None:
    store, sessions, capacity, adapter, manager = await build_manager(tmp_path, limit=2)
    selected = identity(1)
    digest = "a" * 64
    first = await manager.submit(
        identity=selected,
        client_message_id="message-00000001",
        payload_digest=digest,
        prompt="hello",
        source="web",
    )
    repeated = await manager.submit(
        identity=selected,
        client_message_id="message-00000001",
        payload_digest=digest,
        prompt="hello",
        source="web",
    )
    assert first.created
    assert not repeated.created
    assert repeated.run.run_id == first.run.run_id
    with pytest.raises(IdempotencyConflict):
        await manager.submit(
            identity=selected,
            client_message_id="message-00000001",
            payload_digest="b" * 64,
            prompt="changed",
            source="web",
        )
    second = await manager.submit(
        identity=selected,
        client_message_id="message-00000002",
        payload_digest="c" * 64,
        prompt="second",
        source="web",
    )
    assert second.run.state == "QUEUED"

    for _ in range(100):
        if selected.session_id in adapter.release_by_session:
            break
        await asyncio.sleep(0.01)
    adapter.release_by_session[selected.session_id].set()
    task = manager._contexts[first.run.run_id].task
    assert task is not None
    await task
    await wait_until(
        lambda: second.run.run_id in manager._contexts
        and selected.session_id in adapter.release_by_session
    )
    adapter.release_by_session[selected.session_id].set()
    second_task = manager._contexts[second.run.run_id].task
    assert second_task is not None
    await second_task
    snapshot = await store.load_run_snapshot(first.run.run_id)
    assert snapshot is not None
    assert snapshot.run.state == "COMPLETED"
    assert snapshot.answer == "完成"
    assert [
        item.projection_id for item in await store.load_projection_batch()
    ] == [
        f"{first.run.run_id}:queued",
        f"{first.run.run_id}:accepted",
        f"{second.run.run_id}:queued",
        f"{first.run.run_id}:event:1",
        f"{first.run.run_id}:event:3",
        f"{first.run.run_id}:event:4",
        f"{first.run.run_id}:terminal",
        f"{second.run.run_id}:accepted",
        f"{second.run.run_id}:event:1",
        f"{second.run.run_id}:event:3",
        f"{second.run.run_id}:event:4",
        f"{second.run.run_id}:terminal",
    ]
    assert capacity.inflight == 0
    await manager.close()
    await sessions.close()
    await store.close()
