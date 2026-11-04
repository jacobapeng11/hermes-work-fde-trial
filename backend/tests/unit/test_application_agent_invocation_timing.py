from __future__ import annotations

from contextlib import nullcontext
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

import app.services.application_agent_invocation as invocation_module

from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.events import PlatformStreamEvent
from app.models import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
)
from app.services.application_agent_invocation import (
    ApplicationAgentInvocationError,
    ApplicationAgentSubject,
    SqlAlchemyApplicationAgentInvocationService,
    _safe_timing_correlation_id,
)
from app.services.runtime_chat_timing import RuntimeChatTiming
from app.services.runtime_commands import RuntimeCommandError


@pytest.fixture(autouse=True)
def _runtime_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def generation(_db: object) -> int:
        return 11

    monkeypatch.setattr(
        invocation_module,
        "load_runtime_command_admission_generation",
        generation,
    )


def test_application_timing_correlation_ids_are_log_safe() -> None:
    assert (
        _safe_timing_correlation_id("app_run_safe-1:turn.2")
        == "app_run_safe-1:turn.2"
    )
    assert _safe_timing_correlation_id("unsafe\nmessage") is None
    assert _safe_timing_correlation_id("x" * 129) is None


def test_application_channel_plan_supports_runtime_default_model() -> None:
    plan = SqlAlchemyApplicationAgentInvocationService._channel_session_plan(
        agent=SimpleNamespace(
            id=29,
            org_id=1,
            config={},
            config_generation=4,
        ),
        binding=SimpleNamespace(
            runtime_id="application-agent-29",
            config_generation=5,
            endpoint_base_url="http://runtime.test",
            endpoint_host_header="runtime.test",
            runtime_root_host_path="/data/application-agent-29",
        ),
        client=SimpleNamespace(id=23),
        conversation=SimpleNamespace(
            id=101,
            runtime_session_id="app-29-session",
            prompt_contract_version=0,
            prompt_contract_digest=None,
        ),
        conversation_created=True,
        subject=ApplicationAgentSubject(
            subject_type="user",
            subject_key="user:9",
            user_id=9,
        ),
            endpoint=RuntimeEndpoint(
                runtime_workspace_id=29,
                runtime_id="application-agent-29",
                endpoint_base_url="http://runtime.test",
            endpoint_host_header="runtime.test",
            api_key="test-only",
            coordination_scope_key="application:application-agent-29:29",
        ),
        model_entry_id=None,
        config_generation=5,
    )

    assert plan.model_entry_id is None
    assert plan.runtime_session_id == "app-29-session"
    assert len(plan.prompt_contract_digest) == 64


@pytest.mark.asyncio
async def test_application_terminal_projection_is_idempotent() -> None:
    binding = SimpleNamespace(active_run_count=1)
    run = SimpleNamespace(
        status="running",
        submission_state="accepted",
        active_session_key="session-1",
        finished_at=None,
        error_code=None,
        capacity_released_at=None,
        turn_id=None,
    )
    conversation = SimpleNamespace(last_active_at=None)

    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one(self) -> object:
            return self.value

        def scalar_one_or_none(self) -> object:
            return self.value

    class Db:
        def __init__(self) -> None:
            self.values = [
                run,
                conversation,
                run,
                conversation,
            ]
            self.statements: list[str] = []
            self.commits = 0
            self.no_autoflush = nullcontext()

        async def execute(self, statement: object) -> Result:
            self.statements.append(str(statement))
            return Result(self.values.pop(0))

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            raise AssertionError("terminal transaction must not roll back")

    db = Db()
    service = SqlAlchemyApplicationAgentInvocationService(  # type: ignore[arg-type]
        db,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )

    for _ in range(2):
        await service._finalize_invocation_run(
            run_id=60,
            conversation_id=50,
            terminal_status="completed",
            error_code=None,
            turn_id="turn-1",
        )

    assert binding.active_run_count == 1
    assert run.status == "completed"
    assert run.submission_state == "terminal"
    assert run.active_session_key is None
    assert run.capacity_released_at is None
    assert run.turn_id == "turn-1"
    assert conversation.last_active_at is not None
    assert db.commits == 2
    assert "application_agent_run" in db.statements[0]
    assert "application_agent_conversation" in db.statements[1]


class _FakeDb:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.added: list[object] = []

    def add(self, value: object) -> None:
        if isinstance(value, ApplicationAgentRun) and value.id is None:
            value.id = 1
        self.added.append(value)

    async def commit(self) -> None:
        self.order.append("db_commit")

    async def rollback(self) -> None:
        self.order.append("db_rollback")


class _IntegrityRetryDb(_FakeDb):
    def __init__(
        self,
        order: list[str],
        existing_run: ApplicationAgentRun,
    ) -> None:
        super().__init__(order)
        self.existing_run = existing_run
        self.commit_attempts = 0
        self.refreshed: list[object] = []

    async def commit(self) -> None:
        self.commit_attempts += 1
        self.order.append("db_commit")
        if self.commit_attempts == 1:
            raise IntegrityError(
                "insert application_agent_run",
                {},
                RuntimeError("duplicate"),
            )

    async def refresh(self, value: object) -> None:
        self.refreshed.append(value)

    async def execute(self, _statement: object) -> object:
        existing_run = self.existing_run

        class Result:
            def scalar_one_or_none(self) -> ApplicationAgentRun:
                return existing_run

        return Result()


class _FakeRuntimeCommandClient:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def send(self, *, envelope: object, **_kwargs: object) -> object:
        self.order.append("runtime_command_sent")
        payload = getattr(envelope, "payload")
        return SimpleNamespace(
            state="accepted",
            run_id=getattr(payload, "run_id"),
            error_code=None,
        )

    async def wait_run(
        self,
        *,
        run_id: str,
        event_sink: object,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.order.append("runtime_stream_entered")
        sink = event_sink
        await sink("chat.run.started", {"turn_id": "turn-1", "seq": 1})
        await sink(
            "chat.reasoning.delta",
            {"delta": "首个可见输出", "seq": 2},
        )
        await sink("chat.done", {"status": "completed", "seq": 3})
        return {"status": "COMPLETED", "answer": "", "run_id": run_id}

    async def close(self) -> None:
        return None


class _UnavailableRuntimeCommandClient(_FakeRuntimeCommandClient):
    async def send(self, *, envelope: object, **_kwargs: object) -> object:
        del envelope
        self.order.append("runtime_command_failed")
        raise RuntimeCommandError("Runtime command delivery failed")


class _NoopPreparedKnowledge:
    async def prepare_application_agent(
        self,
        **_kwargs: object,
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_channel_fast_lane_plan_uses_current_model_without_creating_run(
) -> None:
    order: list[str] = []
    db = _FakeDb(order)
    service = SqlAlchemyApplicationAgentInvocationService(  # type: ignore[arg-type]
        db,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )
    service._prepare_channel_context = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            agent=SimpleNamespace(id=25, org_id=1),
            binding=SimpleNamespace(
                id=30,
                runtime_id="application-agent-25",
                endpoint_host_header="application-agent-25.test",
            ),
            client=SimpleNamespace(id=35),
            subject=ApplicationAgentSubject(
                subject_type="user",
                subject_key="user:101",
                user_id=101,
            ),
            plan=SimpleNamespace(
                model_entry_id="21",
                runtime_session_id="app-25-session-a",
                config_generation=18,
                endpoint=RuntimeEndpoint(
                    runtime_workspace_id=25,
                    runtime_id="application-agent-25",
                    endpoint_base_url="http://runtime.test",
                    endpoint_host_header="application-agent-25.test",
                    api_key="test-only",
                ),
            ),
        )
    )
    service._resolve_model_route = AsyncMock(  # type: ignore[method-assign]
        return_value=("qwen-max", 128000, {}, None)
    )

    result = await service.prepare_channel_fast_lane(
        application_agent_id=25,
        application_client_id=35,
        delegated_principal=SimpleNamespace(org_id=1, user_id=101),
        conversation_key="application:25:direct:channel:3:user:101",
    )

    assert result.application_agent_id == 25
    assert result.application_client_id == 35
    assert result.subject_user_id == 101
    assert result.runtime_session_id == "app-25-session-a"
    assert result.config_generation == 18
    assert result.target.runtime_id == "application-agent-25"
    assert result.target.admission_generation == 11
    assert result.identity.model_id == "21"
    assert result.identity.upstream_model_id == "qwen-max"
    assert result.identity.config_revision == "generation-18"
    assert order == ["db_commit"]
    assert db.added == []


class _TimingInvocationService(
    SqlAlchemyApplicationAgentInvocationService
):
    def __init__(
        self,
        db: _FakeDb,
        order: list[str],
        prepared_knowledge_service: object,
    ) -> None:
        super().__init__(
            db,
            prepared_knowledge_service=prepared_knowledge_service,
        )  # type: ignore[arg-type]
        self.order = order

    async def _finalize_invocation_run(
        self,
        **_kwargs: object,
    ) -> None:
        self.order.append("capacity_released")

    def _runtime_command_client(self) -> _FakeRuntimeCommandClient:
        return _FakeRuntimeCommandClient(self.order)

    async def _application_model_entry_id(
        self,
        agent: ApplicationAgent,
        *,
        config: dict[str, object] | None = None,
    ) -> str:
        effective_config = agent.config if config is None else config
        return str((effective_config or {})["model_entry_id"])

    def _endpoint(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
    ) -> RuntimeEndpoint:
        return RuntimeEndpoint(
            runtime_workspace_id=int(agent.id),
            runtime_id=str(binding.runtime_id),
            endpoint_base_url="http://runtime.test",
            endpoint_host_header="runtime.test",
            api_key="test-only",
        )


class _UnavailableTimingInvocationService(_TimingInvocationService):
    def _runtime_command_client(self) -> _UnavailableRuntimeCommandClient:
        return _UnavailableRuntimeCommandClient(self.order)


@pytest.mark.asyncio
async def test_text_only_openapi_invocation_skips_runtime_file_listing() -> None:
    order: list[str] = []
    db = _FakeDb(order)
    service = _TimingInvocationService(
        db,
        order,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )
    collections: list[dict[str, object]] = []

    async def collect_files(**kwargs: object) -> None:
        collections.append(dict(kwargs))

    service._collect_openapi_produced_files = collect_files  # type: ignore[method-assign]
    conversation = SimpleNamespace(
        id=101,
        runtime_session_id="app-openapi-session",
        prompt_contract_version=0,
        prompt_contract_digest=None,
    )

    result = await service._execute_invocation(
        agent=ApplicationAgent(
            id=25,
            org_id=1,
            slug="text-only-agent",
            name="Text Only Agent",
            config={"model_entry_id": 7},
            config_generation=1,
        ),
        binding=ApplicationAgentRuntimeBinding(
            id=30,
            runtime_id="application-agent-25",
            config_generation=1,
        ),
        client=None,
        conversation=conversation,
        subject=ApplicationAgentSubject(
            subject_type="service",
            subject_key="service:client",
            service_id="client",
        ),
        runtime_session_id="app-openapi-session",
        request_id="text-only-request",
        input_text="只要文本",
        conversation_key="default",
        channel="openapi",
        create_session=False,
    )

    assert result.output_text == ""
    assert collections == []


@pytest.mark.asyncio
async def test_application_invocation_remains_retryable_when_admission_is_unavailable() -> None:
    order: list[str] = []
    db = _FakeDb(order)
    service = _UnavailableTimingInvocationService(
        db,
        order,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )

    with pytest.raises(
        ApplicationAgentInvocationError,
        match="application_agent_runtime_unavailable",
    ):
        await service._execute_invocation(
            agent=ApplicationAgent(
                id=25,
                org_id=1,
                slug="retryable-agent",
                name="Retryable Agent",
                config={"model_entry_id": 7},
                config_generation=1,
            ),
            binding=ApplicationAgentRuntimeBinding(
                id=30,
                runtime_id="application-agent-25",
                config_generation=1,
            ),
            client=None,
            conversation=None,
            subject=ApplicationAgentSubject(
                subject_type="user",
                subject_key="user:1",
                user_id=1,
            ),
            runtime_session_id="studio-25-1-test",
            request_id="retryable-admission-request",
            input_text="你好",
            conversation_key=None,
            channel="studio",
            create_session=True,
            idempotency_key="retryable-admission-request",
            timing=RuntimeChatTiming(),
        )

    run = next(
        value
        for value in db.added
        if isinstance(value, ApplicationAgentRun)
    )
    assert run.status == "queued"
    assert run.submission_state == "reserved"
    assert run.submit_intent_at is None
    assert run.dashboard_accepted_at is None
    assert "capacity_released" not in order


@pytest.mark.asyncio
async def test_application_invocation_refreshes_context_before_idempotent_resume() -> None:
    order: list[str] = []
    runtime_run_id = "wecom_run_retryable"
    existing = ApplicationAgentRun(
        id=393,
        org_id=1,
        application_agent_id=25,
        runtime_binding_id=30,
        application_client_id=None,
        conversation_id=None,
        runtime_session_id="app-25-session",
        active_session_key="app-25-session",
        runtime_run_id=runtime_run_id,
        subject_type="user",
        subject_user_id=1,
        subject_service_id=None,
        channel="studio",
        status="queued",
        request_id="retryable-request",
        idempotency_key="retryable-request",
        payload_digest=None,
        submission_state="reserved",
        submit_intent_at=None,
    )
    db = _IntegrityRetryDb(order, existing)
    service = _TimingInvocationService(
        db,
        order,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )
    agent = ApplicationAgent(
        id=25,
        org_id=1,
        slug="retryable-agent",
        name="Retryable Agent",
        config={"model_entry_id": 7},
        config_generation=1,
    )
    binding = ApplicationAgentRuntimeBinding(
        id=30,
        runtime_id="application-agent-25",
        config_generation=1,
    )

    result = await service._execute_invocation(
        agent=agent,
        binding=binding,
        client=None,
        conversation=None,
        subject=ApplicationAgentSubject(
            subject_type="user",
            subject_key="user:1",
            user_id=1,
        ),
        runtime_session_id="app-25-session",
        request_id="retryable-request",
        input_text="你好",
        conversation_key=None,
        channel="studio",
        create_session=False,
        idempotency_key="retryable-request",
        deterministic_runtime_run_id=runtime_run_id,
        timing=RuntimeChatTiming(),
    )

    assert result.runtime_session_id == "app-25-session"
    assert db.refreshed == [agent, binding]
    assert "runtime_command_sent" in order


@pytest.mark.asyncio
async def test_application_invocation_records_stages_and_notifies_submission(
    caplog: pytest.LogCaptureFixture,
) -> None:
    order: list[str] = []
    db = _FakeDb(order)
    service = _TimingInvocationService(
        db,
        order,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )
    clock_value = 0.0

    def clock() -> float:
        nonlocal clock_value
        clock_value += 0.01
        return clock_value

    timing = RuntimeChatTiming(clock=clock)
    timing.mark("invocation_authorized")
    timing.mark("conversation_resolved")
    events: list[str] = []

    async def emit(event: PlatformStreamEvent) -> None:
        events.append(event.event)

    def submitted() -> None:
        order.append("runtime_submitted_sink")

    caplog.set_level(
        logging.INFO,
        logger="uvicorn.error.runtime_timing",
    )
    result = await service._execute_invocation(
        agent=ApplicationAgent(
            id=25,
            org_id=1,
            slug="timing-agent",
            name="Timing Agent",
            config={
                "model_entry_id": 7,
            },
            config_generation=1,
        ),
        binding=ApplicationAgentRuntimeBinding(
            id=30,
            runtime_id="application-agent-25",
            config_generation=1,
        ),
        client=None,
        conversation=None,
        subject=ApplicationAgentSubject(
            subject_type="user",
            subject_key="user:1",
            user_id=1,
        ),
        runtime_session_id="studio-25-1-test",
        request_id="studio-web-safe-request",
        input_text="不得写入 timing 日志",
        conversation_key=None,
        channel="studio",
        create_session=True,
        event_sink=emit,
        runtime_submitted_sink=submitted,
        timing=timing,
    )

    assert result.output_text == ""
    assert events == [
        "chat.run.accepted",
        "chat.run.started",
        "chat.reasoning.delta",
        "chat.done",
    ]
    assert order.index("runtime_submitted_sink") < order.index(
        "runtime_stream_entered"
    )
    assert timing.duration_ms(
        "prompt_ack",
        "first_visible_event_received",
    ) == 10.0
    assert "application agent invocation timing=" in caplog.text
    assert "studio-web-safe-request" in caplog.text
    assert "不得写入 timing 日志" not in caplog.text


@pytest.mark.asyncio
async def test_application_prepared_context_gates_internal_turn(
) -> None:
    order: list[str] = []
    preparation_started = asyncio.Event()
    release_preparation = asyncio.Event()

    class PreparedKnowledge:
        async def prepare_application_agent(
            self,
            **kwargs: object,
        ) -> None:
            order.append("prepared_context_started")
            run = next(
                value
                for value in db.added
                if isinstance(value, ApplicationAgentRun)
            )
            assert run.status == "running"
            assert order.count("db_commit") >= 2
            runtime_run_id = kwargs.pop("runtime_run_id")
            assert str(runtime_run_id).startswith("app_run_")
            assert kwargs == {
                "application_agent_id": 25,
                "runtime_session_id": "studio-25-1-test",
                "runtime_token": "test-only",
            }
            preparation_started.set()
            await release_preparation.wait()
            order.append("prepared_context_ready")

    db = _FakeDb(order)
    service = _TimingInvocationService(
        db,
        order,
        prepared_knowledge_service=PreparedKnowledge(),
    )

    task = asyncio.create_task(
        service._execute_invocation(
            agent=ApplicationAgent(
                id=25,
                org_id=1,
                slug="timing-agent",
                name="Timing Agent",
                config={"model_entry_id": 7},
                config_generation=1,
            ),
            binding=ApplicationAgentRuntimeBinding(
                id=30,
                runtime_id="application-agent-25",
                config_generation=1,
            ),
            client=None,
            conversation=None,
            subject=ApplicationAgentSubject(
                subject_type="user",
                subject_key="user:1",
                user_id=1,
            ),
            runtime_session_id="studio-25-1-test",
            request_id="studio-prepared-context",
            input_text="你好",
            conversation_key=None,
            channel="studio",
            create_session=True,
            timing=RuntimeChatTiming(),
        )
    )
    await preparation_started.wait()
    assert "runtime_command_sent" not in order
    release_preparation.set()
    await task

    assert order.index("prepared_context_ready") < order.index(
        "runtime_command_sent"
    )


@pytest.mark.asyncio
async def test_service_subject_skips_delegated_user_knowledge_preparation() -> None:
    order: list[str] = []

    class PreparedKnowledge:
        async def prepare_application_agent(
            self,
            **_kwargs: object,
        ) -> None:
            raise AssertionError(
                "service subject must not prepare delegated-user knowledge"
            )

    service = _TimingInvocationService(
        _FakeDb(order),
        order,
        prepared_knowledge_service=PreparedKnowledge(),
    )

    await service._execute_invocation(
        agent=ApplicationAgent(
            id=25,
            org_id=1,
            slug="service-agent",
            name="Service Agent",
            config={"model_entry_id": 7},
            config_generation=1,
        ),
        binding=ApplicationAgentRuntimeBinding(
            id=30,
            runtime_id="application-agent-25",
            config_generation=1,
        ),
        client=None,
        conversation=None,
        subject=ApplicationAgentSubject(
            subject_type="service",
            subject_key="service:client-1",
            service_id="client-1",
        ),
        runtime_session_id="app-25-service-session",
        request_id="service-route-prepared",
        input_text="检查状态",
        conversation_key=None,
        channel="openapi",
        create_session=False,
        timing=RuntimeChatTiming(),
    )

    assert "runtime_command_sent" in order
