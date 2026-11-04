"""Contract tests for the scheduled-task execution orchestrator.

The fakes deliberately model the seams around RuntimeWorkspace and
RuntimeChatService so the tests do not require a real Runtime or a wall-clock
sleep.  The implementation is expected to keep all these callbacks short-
lived and to make no database transaction span a keepalive or stream.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.hermes_runtime.schemas import RuntimeEndpoint
from app.schemas.runtime_internal import RuntimeCommandAck, RuntimeTurnIdentity
from app.services.runtime_admission import RuntimeAdmissionError, RuntimeAdmissionService
from app.services.runtime_chat import (
    CreatedRuntimeRun,
    RuntimeChatActiveRunError,
    RuntimeChatDatabaseBusyError,
    RuntimeChatPreparationError,
)
from app.services.runtime_workspace import Principal, RuntimeWorkspaceRecord
from app.services.runtime_commands import (
    RuntimeCommandTarget,
    deterministic_command_id,
)
from app.services.runtime_internal_turns import (
    PersonalInternalTurnContext,
    submit_personal_internal_turn,
)
from app.services.scheduled_task_launcher import RunnerRequest, TaskSnapshot
from app.services.scheduled_task_runner import (
    RuntimeRunTerminal,
    ScheduledTaskRunner,
    ScheduledTaskRunnerError,
)


def _request(*, trigger_kind: str = "scheduled", status: str = "active", next_run_at=None) -> RunnerRequest:
    snapshot = TaskSnapshot(
        task_id=7,
        task_name="daily",
        prompt="summarize",
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        target_session_id=4,
        target_runtime_session_id="rs-7",
        target_session_status="active",
        status=status,
        schedule_type="once",
        schedule_config={"type": "once"},
        next_run_at=next_run_at,
    )
    return RunnerRequest(7, trigger_kind, next_run_at, snapshot)


def _endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=3,
        runtime_id="rt-3",
        endpoint_base_url="http://runtime.example",
        endpoint_host_header="runtime.example",
        api_key="runtime-key",
    )


@pytest.mark.asyncio
async def test_internal_turn_accepts_a_confirmed_coalesced_run_id() -> None:
    class Client:
        def __init__(self) -> None:
            self.envelope = None

        async def send(self, **kwargs):
            self.envelope = kwargs["envelope"]
            return RuntimeCommandAck(
                command_id="command-canonical",
                state="accepted",
                run_id="rr-existing-candidate",
                result={
                    "admission_state": "queued",
                    "coalesced": True,
                    "canonical_run_id": "rr-existing-candidate",
                },
            )

    context = PersonalInternalTurnContext(
        target=RuntimeCommandTarget(
            runtime_id="runtime-1",
            binding_id="binding-1",
            endpoint_host_header="runtime.example",
            api_server_key="secret",
            admission_generation=11,
        ),
        identity=RuntimeTurnIdentity(
            user_id=2,
            org_id=1,
            session_id="rs-7",
            profile_id="default",
            runtime_agent_profile_id=51,
            profile_desired_revision=1,
            profile_applied_revision=1,
            profile_digest="sha256:" + "1" * 64,
            skill_digest="sha256:" + "2" * 64,
            mcp_digest="sha256:" + "3" * 64,
            knowledge_digest="sha256:" + "4" * 64,
            model_id="9",
            config_revision="revision-18",
            admission_generation=11,
            max_prompt_bytes=1024,
            max_output_tokens=4096,
        ),
    )
    client = Client()
    result = await submit_personal_internal_turn(
        context=context,
        created=CreatedRuntimeRun(3, "rs-7", "rr-new-occurrence", 9, "queued"),
        prompt="summarize",
        source="scheduled_task",
        execution_key="scheduled:7:rr-new-occurrence",
        scheduled_task_id=7,
        scheduled_occurrence_key="scheduled:7:new-occurrence",
        command_id_override="cmd_recovery_7",
        client=client,  # type: ignore[arg-type]
    )

    assert result["canonical_run_id"] == "rr-existing-candidate"
    assert result["coalesced"] is True
    assert client.envelope.command_id == "cmd_recovery_7"
    assert client.envelope.payload.client_message_id == deterministic_command_id(
        "internal-turn-message",
        "scheduled_task",
        "scheduled:7:rr-new-occurrence",
    )


@pytest.mark.asyncio
async def test_recovery_resubmits_original_run_after_supervisor_404(
    monkeypatch,
) -> None:
    from app.services import scheduled_task_runner as module
    from app.services.scheduled_task_execution_leases import (
        ScheduledTaskExecutionGrant,
    )

    fire_at = datetime(2026, 8, 27, 4, 38, 3, tzinfo=timezone.utc)
    runtime_run_id = "rr-scheduled:7:orphan"
    source_detail = {
        "type": "scheduled",
        "scheduled_task_id": 7,
        "scheduled_task_name": "original name",
        "trigger_kind": "scheduled",
        "task_available": True,
    }
    run = SimpleNamespace(
        id=88,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        runtime_session_id="rs-7",
        runtime_run_id=runtime_run_id,
        model_entry_id=9,
        runtime_agent_profile_id=None,
        status="queued",
        error_code=None,
        error_message=None,
        created_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        request_payload={
            "message": "immutable original prompt",
            "runtime_agent_profile_id": 51,
            "hermes_profile_name": "default",
            "source": source_detail,
        },
    )

    class Session:
        async def scalar(self, _statement):
            return run

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    target = RuntimeCommandTarget(
        runtime_id="runtime-1",
        binding_id="binding-1",
        endpoint_host_header="runtime.example",
        api_server_key="secret",
        admission_generation=11,
    )
    context = SimpleNamespace(target=target, identity="identity")

    async def load_target(*_args, **_kwargs):
        return target

    async def resolve(*_args, **_kwargs):
        return context

    class MissingRunClient:
        async def get_run(self, **kwargs):
            assert kwargs["target"] is target
            assert kwargs["run_id"] == runtime_run_id
            return None

        async def close(self):
            return None

    submissions: list[dict] = []

    async def submit(**kwargs):
        submissions.append(kwargs)
        return {"admission_state": "queued"}

    monkeypatch.setattr(module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        module,
        "load_personal_runtime_command_target",
        load_target,
    )
    monkeypatch.setattr(module, "RuntimeCommandClient", MissingRunClient)
    monkeypatch.setattr(module, "resolve_personal_internal_turn", resolve)
    monkeypatch.setattr(module, "submit_personal_internal_turn", submit)
    monkeypatch.setattr(
        module.settings,
        "RUNTIME_RUN_RECONCILE_AFTER_SECONDS",
        0,
    )

    grant = ScheduledTaskExecutionGrant(
        task_id=7,
        claim_token="token",
        owner_instance_id="backend-a",
        lease_epoch=2,
        trigger_kind="scheduled",
        scheduled_fire_at=fire_at,
        runtime_run_id=runtime_run_id,
    )
    request = RunnerRequest.with_execution_grant(
        task_id=7,
        trigger_kind="scheduled",
        scheduled_fire_at=fire_at,
        task_snapshot=_request(next_run_at=fire_at).task_snapshot,
        grant=grant,
    )
    runner = ScheduledTaskRunner(
        runtime_activator=lambda *_args: _async_value(_endpoint()),
        session_admission_timeout_seconds=0,
    )

    terminal = await runner._recover_claimed_runtime_run(
        request=request,
        principal=Principal(org_id=1, user_id=2),
    )

    assert terminal == RuntimeRunTerminal(runtime_run_id, "queued")
    assert len(submissions) == 1
    submission = submissions[0]
    assert submission["prompt"] == "immutable original prompt"
    assert submission["source_detail"] == source_detail
    assert submission["created"].runtime_run_id == runtime_run_id
    assert submission["created"].runtime_agent_profile_id == 51
    assert submission["execution_key"] == f"scheduled:7:{runtime_run_id}"
    assert submission["command_id_override"] == deterministic_command_id(
        "internal-turn-recovery",
        "scheduled_task",
        "binding-1",
        f"scheduled:7:{runtime_run_id}",
    )

    class ExistingRunClient:
        async def get_run(self, **_kwargs):
            return {"run_id": runtime_run_id, "status": "RUNNING"}

        async def close(self):
            return None

    monkeypatch.setattr(module, "RuntimeCommandClient", ExistingRunClient)
    monkeypatch.setattr(
        module.settings,
        "RUNTIME_RUN_RECONCILE_AFTER_SECONDS",
        -30,
    )
    submissions.clear()

    with pytest.raises(module.ScheduledTaskExecutionRecoveryPending):
        await runner._recover_claimed_runtime_run(
            request=request,
            principal=Principal(org_id=1, user_id=2),
        )

    assert submissions == []

    class FailedQueryClient:
        async def get_run(self, **_kwargs):
            raise module.RuntimeCommandError("query unavailable")

        async def close(self):
            return None

    monkeypatch.setattr(module, "RuntimeCommandClient", FailedQueryClient)

    with pytest.raises(module.ScheduledTaskExecutionRecoveryPending):
        await runner._recover_claimed_runtime_run(
            request=request,
            principal=Principal(org_id=1, user_id=2),
        )

    assert submissions == []


@pytest.mark.asyncio
async def test_application_runtime_ready_path_does_not_invoke_warmup(monkeypatch) -> None:
    from app.services import scheduled_task_runner as module

    profile = RuntimeWorkspaceRecord(
        runtime_workspace_id=3,
        cloud_profile_id=3,
        org_id=1,
        user_id=2,
        user_name="User",
        user_code="employee-2",
        name="Workspace",
        status="active",
        runtime_status="ready",
    )
    endpoint = RuntimeEndpoint(
        runtime_workspace_id=3,
        runtime_id="rt-3",
        endpoint_base_url="http://runtime.example",
        endpoint_host_header="runtime.example",
        api_key="runtime-key",
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class WorkspaceService:
        async def ensure_default_for_user(self, _principal):
            return profile

    class WorkspaceStore:
        async def get_runtime_endpoint(self, record):
            assert record is profile
            return endpoint

    class ForbiddenWarmup:
        async def warmup(self, _record):
            raise AssertionError("ready binding must not invoke Runtime warmup")

    infrastructure = SimpleNamespace(
        workspace_service=WorkspaceService(),
        workspace_store=WorkspaceStore(),
        warmup_runner=ForbiddenWarmup(),
    )
    monkeypatch.setattr(module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        module,
        "build_runtime_infrastructure",
        lambda _db: infrastructure,
    )

    result = await ScheduledTaskRunner._activate_application_runtime(
        Principal(org_id=1, user_id=2),
        _request().task_snapshot,
    )

    assert result is endpoint


@pytest.mark.asyncio
async def test_application_runtime_preserves_retryable_admission_code(monkeypatch) -> None:
    from app.services import scheduled_task_runner as module

    profile = RuntimeWorkspaceRecord(
        runtime_workspace_id=3,
        cloud_profile_id=3,
        org_id=1,
        user_id=2,
        user_name="User",
        user_code="employee-2",
        name="Workspace",
        status="active",
        runtime_status="starting",
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class WorkspaceService:
        async def ensure_default_for_user(self, _principal):
            return profile

    class Warmup:
        async def warmup(self, _record):
            raise RuntimeAdmissionError(RuntimeAdmissionService.decide("starting"))

    infrastructure = SimpleNamespace(
        workspace_service=WorkspaceService(),
        workspace_store=object(),
        warmup_runner=Warmup(),
    )
    monkeypatch.setattr(module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        module,
        "build_runtime_infrastructure",
        lambda _db: infrastructure,
    )

    with pytest.raises(ScheduledTaskRunnerError) as raised:
        await ScheduledTaskRunner._activate_application_runtime(
            Principal(org_id=1, user_id=2),
            _request().task_snapshot,
        )

    assert str(raised.value) == "RUNTIME_STARTING"


@pytest.mark.asyncio
async def test_runner_activates_ready_runtime_creates_and_executes_run() -> None:
    calls: list[str] = []

    async def load_task(_request):
        calls.append("task")
        return SimpleNamespace(status="active", next_run_at=_request.scheduled_fire_at, target_session_status="active")

    async def activate(_principal, _snapshot):
        calls.append("activate")
        return "endpoint"

    async def create_run(_principal, _snapshot, _endpoint):
        calls.append("admit")
        return CreatedRuntimeRun(3, "rs-7", "rr-7", None, "running")

    async def execute(_created, _snapshot, _endpoint):
        calls.append("execute")
        return RuntimeRunTerminal(runtime_run_id="rr-7", status="completed")

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=activate,
        run_creator=create_run,
        run_executor=execute,
    )
    await runner.run(_request(next_run_at=datetime.now(timezone.utc)))
    assert calls == ["task", "activate", "task", "admit", "execute"]


@pytest.mark.asyncio
async def test_scheduled_runner_does_not_retry_session_busy_before_supervisor_receipt() -> None:
    attempts = 0

    async def create_run(_principal, _snapshot, _endpoint):
        nonlocal attempts
        attempts += 1
        raise RuntimeChatActiveRunError("busy")

    async def load_task(_request):
        return SimpleNamespace(status="active", next_run_at=_request.scheduled_fire_at, target_session_status="active")

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=lambda *_: _async_value("endpoint"),
        run_creator=create_run,
        run_executor=lambda *_: _async_value(RuntimeRunTerminal("rr-7", "completed")),
        session_retry_interval_seconds=0,
    )
    with pytest.raises(RuntimeChatActiveRunError):
        await runner.wait_for_session_admission(
            request=_request(next_run_at=datetime.now(timezone.utc))
        )
    assert attempts == 1


@pytest.mark.asyncio
async def test_scheduled_runner_does_not_duplicate_database_busy_admission() -> None:
    attempts = 0

    async def create_run(_principal, _snapshot, _endpoint):
        nonlocal attempts
        attempts += 1
        raise RuntimeChatDatabaseBusyError(
            mysql_error_code=1213
        )

    async def load_task(_request):
        return SimpleNamespace(
            status="active",
            next_run_at=_request.scheduled_fire_at,
            target_session_status="active",
        )

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=lambda *_: _async_value("endpoint"),
        run_creator=create_run,
        session_retry_interval_seconds=0,
    )

    with pytest.raises(RuntimeChatDatabaseBusyError):
        await runner.wait_for_session_admission(
            request=_request(next_run_at=datetime.now(timezone.utc))
        )
    assert attempts == 1


@pytest.mark.asyncio
async def test_manual_runner_stops_busy_admission_and_finalizes_session_busy() -> None:
    summaries: list[RuntimeRunTerminal] = []

    async def load_task(_request):
        return SimpleNamespace(status="active", next_run_at=_request.scheduled_fire_at, target_session_status="active")

    async def busy(*_args):
        raise RuntimeChatActiveRunError("runtime session already has an active run")

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=lambda *_: _async_value("endpoint"),
        run_creator=busy,
        task_updater=lambda _request, terminal: summaries.append(terminal),
        session_retry_interval_seconds=0,
        session_admission_timeout_seconds=0,
    )

    await runner.run(_request(trigger_kind="manual", next_run_at=datetime.now(timezone.utc)))

    assert len(summaries) == 1
    assert summaries[0].error_code == "SESSION_BUSY"
    assert "active run" in str(summaries[0].error_message)


@pytest.mark.asyncio
async def test_application_executor_releases_database_session_before_internal_turn(
    monkeypatch,
) -> None:
    from app.services import scheduled_task_runner as module

    sessions: list[str] = []
    exits: list[str] = []

    class SessionContext:
        async def __aenter__(self):
            marker = f"session-{len(sessions) + 1}"
            sessions.append(marker)
            self.marker = marker
            return marker

        async def __aexit__(self, *_args):
            exits.append(self.marker)

    context = SimpleNamespace(target="target", identity="identity")
    resolutions = []
    executions = []

    async def resolve(db, **kwargs):
        resolutions.append((db, kwargs))
        return context

    async def enqueue(**kwargs):
        assert exits == ["session-1"]
        executions.append(kwargs)
        return {"admission_state": "queued"}

    monkeypatch.setattr(module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(module, "resolve_personal_internal_turn", resolve)
    monkeypatch.setattr(module, "submit_personal_internal_turn", enqueue)

    created = CreatedRuntimeRun(3, "rs-7", "rr-7", 9, "accepted")
    snapshot = _request(
        next_run_at=datetime.now(timezone.utc)
    ).task_snapshot
    endpoint = _endpoint()
    terminal = await ScheduledTaskRunner._execute_application_run(
        created,
        snapshot,
        endpoint=endpoint,
    )

    assert terminal.status == "queued"
    assert resolutions == [(
        "session-1",
        {
            "principal": Principal(user_id=2, org_id=1),
            "created": created,
            "endpoint": endpoint,
        },
    )]
    assert executions == [{
        "context": context,
        "created": created,
        "prompt": "summarize",
        "source": "scheduled_task",
        "execution_key": "scheduled:7:rr-7",
        "source_detail": {
            "type": "scheduled",
            "scheduled_task_id": 7,
            "scheduled_task_name": "daily",
            "trigger_kind": "scheduled",
            "task_available": True,
        },
        "scheduled_task_id": 7,
        "scheduled_occurrence_key": executions[0]["scheduled_occurrence_key"],
    }]
    assert executions[0]["scheduled_occurrence_key"].startswith("scheduled:7:")
    assert sessions == ["session-1"]
    assert exits == sessions


@pytest.mark.asyncio
async def test_application_executor_preserves_manual_trigger_source(
    monkeypatch,
) -> None:
    from app.services import scheduled_task_runner as module

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(target="target", identity="identity")

    submissions: list[dict] = []

    async def enqueue(**kwargs):
        submissions.append(kwargs)
        return {"admission_state": "queued"}

    monkeypatch.setattr(module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(module, "resolve_personal_internal_turn", resolve)
    monkeypatch.setattr(module, "submit_personal_internal_turn", enqueue)

    created = CreatedRuntimeRun(3, "rs-7", "rr-manual-7", 9, "queued")
    await ScheduledTaskRunner._execute_application_run(
        created,
        _request(trigger_kind="manual").task_snapshot,
        endpoint=_endpoint(),
        trigger_kind="manual",
    )

    assert submissions[0]["source_detail"]["trigger_kind"] == "manual"
    assert submissions[0]["scheduled_occurrence_key"].startswith("manual:7:")


@pytest.mark.asyncio
async def test_application_executor_accepts_coalesced_canonical_run(
    monkeypatch,
) -> None:
    from app.services import scheduled_task_runner as module

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(target="target", identity="identity")

    async def enqueue(**_kwargs):
        return {
            "admission_state": "queued",
            "coalesced": True,
            "canonical_run_id": "rr-existing-candidate",
        }

    coalesced: list[tuple[Principal, CreatedRuntimeRun, str]] = []

    async def mark_coalesced(principal, created, canonical_run_id):
        coalesced.append((principal, created, canonical_run_id))

    monkeypatch.setattr(module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(module, "resolve_personal_internal_turn", resolve)
    monkeypatch.setattr(module, "submit_personal_internal_turn", enqueue)
    monkeypatch.setattr(
        ScheduledTaskRunner,
        "_coalesce_application_run",
        staticmethod(mark_coalesced),
    )

    created = CreatedRuntimeRun(3, "rs-7", "rr-new-occurrence", 9, "queued")
    terminal = await ScheduledTaskRunner._execute_application_run(
        created,
        _request().task_snapshot,
        endpoint=_endpoint(),
    )

    assert terminal == RuntimeRunTerminal(
        "rr-existing-candidate",
        "queued",
    )
    assert coalesced == [
        (
            Principal(org_id=1, user_id=2),
            created,
            "rr-existing-candidate",
        )
    ]


@pytest.mark.asyncio
async def test_application_executor_maps_internal_turn_failure_without_broker(
    monkeypatch,
) -> None:
    from app.services import scheduled_task_runner as module

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(target="target", identity="identity")

    async def execute(**_kwargs):
        raise module.RuntimeInternalTurnError("RUNTIME_MODEL_UNAVAILABLE")

    failures = []

    async def fail(principal, created, exc):
        failures.append((principal, created, exc))

    monkeypatch.setattr(module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(module, "resolve_personal_internal_turn", resolve)
    monkeypatch.setattr(module, "submit_personal_internal_turn", execute)
    monkeypatch.setattr(
        ScheduledTaskRunner,
        "_fail_application_run",
        staticmethod(fail),
    )

    created = CreatedRuntimeRun(3, "rs-7", "rr-7", None, "accepted")
    terminal = await ScheduledTaskRunner._execute_application_run(
        created,
        _request(next_run_at=datetime.now(timezone.utc)).task_snapshot,
        endpoint=_endpoint(),
    )

    assert terminal == RuntimeRunTerminal(
        "rr-7",
        "failed",
        "RUNTIME_MODEL_UNAVAILABLE",
    )
    assert len(failures) == 1
    assert failures[0][0] == Principal(user_id=2, org_id=1)
    assert failures[0][1] is created


@pytest.mark.asyncio
async def test_application_factory_ignores_legacy_broker_injection(
    monkeypatch,
) -> None:
    class RecordingBroker:
        async def publish(self, _runtime_run_id, _event) -> None:
            return None

    broker = RecordingBroker()
    calls = []

    async def execute(created, snapshot, endpoint):
        calls.append((created, snapshot, endpoint))
        return RuntimeRunTerminal(created.runtime_run_id, "completed")

    monkeypatch.setattr(ScheduledTaskRunner, "_execute_application_run", staticmethod(execute))
    runner = ScheduledTaskRunner.for_application(broker=broker)
    created = CreatedRuntimeRun(3, "rs-7", "rr-7", None, "accepted")
    snapshot = _request(next_run_at=datetime.now(timezone.utc)).task_snapshot

    result = await runner.run_executor(created, snapshot, "endpoint")

    assert result.status == "completed"
    assert calls == [(created, snapshot, "endpoint")]


@pytest.mark.asyncio
async def test_runner_calls_task_updater_with_terminal_summary() -> None:
    summaries = []

    async def load_task(_request):
        return SimpleNamespace(status="active", next_run_at=_request.scheduled_fire_at, target_session_status="active")

    async def update(request, terminal):
        summaries.append((request.task_id, terminal.runtime_run_id, terminal.status))

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=lambda *_: _async_value("endpoint"),
        run_creator=lambda *_: _async_value(CreatedRuntimeRun(3, "rs-7", "rr-7", None, "running")),
        run_executor=lambda *_: _async_value(RuntimeRunTerminal("rr-7", "completed")),
        task_updater=update,
    )
    await runner.run(_request(next_run_at=datetime.now(timezone.utc)))
    assert summaries == [(7, "rr-7", "completed")]


@pytest.mark.asyncio
async def test_runner_treats_queued_receipt_as_successful_submission() -> None:
    summaries: list[RuntimeRunTerminal] = []

    async def load_task(request):
        return SimpleNamespace(
            status="active",
            next_run_at=request.scheduled_fire_at,
            target_session_status="active",
        )

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=lambda *_: _async_value("endpoint"),
        run_creator=lambda *_: _async_value(
            CreatedRuntimeRun(3, "rs-7", "rr-7", None, "queued")
        ),
        run_executor=lambda *_: _async_value(
            RuntimeRunTerminal("rr-7", "queued")
        ),
        task_updater=lambda _request, terminal: summaries.append(terminal),
    )

    await runner.run(_request(next_run_at=datetime.now(timezone.utc)))

    assert [summary.status for summary in summaries] == ["queued"]
    assert runner.success_count == 1
    assert runner.failure_count == 0


@pytest.mark.asyncio
async def test_runner_cancellation_does_not_finalize_or_increment_failure() -> None:
    finalized = False
    started = asyncio.Event()

    async def activate(*_):
        started.set()
        await asyncio.Event().wait()

    async def update(*_):
        nonlocal finalized
        finalized = True

    runner = ScheduledTaskRunner(
        task_loader=lambda request: _async_value(SimpleNamespace(status="active", next_run_at=request.scheduled_fire_at, target_session_status="active")),
        runtime_activator=activate,
        task_updater=update,
    )
    task = asyncio.create_task(runner.run(_request(next_run_at=datetime.now(timezone.utc))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized is False
    assert runner.failure_count == 0


@pytest.mark.asyncio
async def test_scheduled_runner_prepares_source_checkpoint_before_execution() -> None:
    calls: list[tuple[str, dict]] = []

    class ChatService:
        async def create_run(self, *, principal, intent):
            calls.append(("create", {"source_type": intent.source_type}))
            return CreatedRuntimeRun(3, "rs-7", "rr-7", None, "running")

        async def prepare_scheduled_source(self, *, principal, created, snapshot, trigger_kind):
            calls.append(
                (
                    "checkpoint",
                    {
                        "type": "scheduled",
                        "scheduled_task_id": snapshot.task_id,
                        "scheduled_task_name": snapshot.task_name,
                        "trigger_kind": trigger_kind,
                        "history_message_count_before_run": 7,
                    },
                )
            )

    async def load_task(_request):
        return SimpleNamespace(status="active", next_run_at=_request.scheduled_fire_at, target_session_status="active")

    async def execute(_created, _snapshot, _endpoint):
        calls.append(("execute", {}))
        return RuntimeRunTerminal("rr-7", "completed")

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=lambda *_: _async_value("endpoint"),
        runtime_chat_service=ChatService(),
        run_executor=execute,
        session_retry_interval_seconds=0,
    )

    await runner.run(_request(next_run_at=datetime.now(timezone.utc)))

    assert [name for name, _payload in calls] == ["create", "checkpoint", "execute"]
    assert calls[1][1] == {
        "type": "scheduled",
        "scheduled_task_id": 7,
        "scheduled_task_name": "daily",
        "trigger_kind": "scheduled",
        "history_message_count_before_run": 7,
    }


@pytest.mark.asyncio
async def test_scheduled_runner_checkpoint_failure_marks_run_failed_without_execution() -> None:
    calls: list[str] = []

    class ChatService:
        async def create_run(self, *, principal, intent):
            calls.append("create")
            return CreatedRuntimeRun(3, "rs-7", "rr-7", None, "running")

        async def prepare_scheduled_source(self, **_kwargs):
            calls.append("checkpoint")
            raise RuntimeChatPreparationError("history checkpoint unavailable")

        async def mark_runtime_run_finished(self, **kwargs):
            calls.append("failed")
            assert kwargs["runtime_run_id"] == "rr-7"
            assert kwargs["status"] == "failed"

    async def load_task(_request):
        return SimpleNamespace(status="active", next_run_at=_request.scheduled_fire_at, target_session_status="active")

    async def execute(*_args):
        calls.append("execute")
        return RuntimeRunTerminal("rr-7", "completed")

    runner = ScheduledTaskRunner(
        task_loader=load_task,
        runtime_activator=lambda *_: _async_value("endpoint"),
        runtime_chat_service=ChatService(),
        run_executor=execute,
        session_retry_interval_seconds=0,
    )

    await runner.run(_request(next_run_at=datetime.now(timezone.utc)))

    assert calls == ["create", "checkpoint", "failed"]
    assert runner.failure_count == 1


async def _async_value(value):
    return value
