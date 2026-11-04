from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.services.runtime_chat import (
    CreatedRuntimeRun,
    RuntimeRunControl,
    RuntimeChatPolicy,
    RuntimeChatActiveRunError,
    RuntimeChatAdmissionError,
    RuntimeChatDatabaseBusyError,
    RuntimeChatIdempotencyConflictError,
    RuntimeChatPreparationError,
    RuntimeChatRequestIntent,
    PreparedRuntimeRun,
    RuntimeSessionHistoryControl,
    RuntimeChatService,
    SqlAlchemyRuntimeChatStore,
)
from app.services.asset_visibility import VisibleAssetRef
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.services.runtime_reload import ProjectionScope, ReloadDecisionInput, RuntimeStatus
from app.services.runtime_chat_timing import RuntimeChatTiming
from app.models import RuntimeRun, RuntimeSession
from app.services.runtime_workspace import Principal


class FakeRuntimeChatStore:
    def __init__(self, policy: RuntimeChatPolicy | None) -> None:
        self.policy = policy
        self.requests: list[dict] = []
        self.created_runs = 0
        self.prepared_runs = []
        self.received_timing: RuntimeChatTiming | None = None

    async def get_chat_policy(self, *, org_id: int, user_id: int, runtime_workspace_id: int, runtime_session_id: str):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_workspace_id": runtime_workspace_id,
                "runtime_session_id": runtime_session_id,
            }
        )
        return self.policy

    async def create_runtime_run(
        self,
        *,
        org_id: int,
        user_id: int,
        prepared,
        timing: RuntimeChatTiming | None = None,
    ):
        self.created_runs += 1
        self.prepared_runs.append(prepared)
        self.received_timing = timing
        return CreatedRuntimeRun(
            runtime_workspace_id=prepared.runtime_workspace_id,
            runtime_session_id=prepared.runtime_session_id,
            runtime_run_id="rr-created",
            model_entry_id=prepared.model_entry_id,
            status="accepted",
        )

    async def get_runtime_endpoint(self, *, org_id: int, user_id: int, runtime_workspace_id: int):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_workspace_id": runtime_workspace_id,
                "endpoint": True,
            }
        )
        return RuntimeEndpoint(
            runtime_workspace_id=runtime_workspace_id,
            runtime_id=f"rt-{runtime_workspace_id}",
            endpoint_base_url="http://runtime.example",
            endpoint_host_header="runtime.example",
            api_key="runtime-token",
        )

    async def get_runtime_run_control(self, *, org_id: int, user_id: int, runtime_run_id: str):
        self.requests.append({"org_id": org_id, "user_id": user_id, "runtime_run_id": runtime_run_id})
        return RuntimeRunControl(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            runtime_run_id=runtime_run_id,
            runtime_provider_run_id="run-provider-1",
            model_entry_id=10,
            status="running",
            endpoint=RuntimeEndpoint(
                runtime_workspace_id=123,
                runtime_id="rt-123",
                endpoint_base_url="http://runtime.example",
                endpoint_host_header="runtime.example",
                api_key="runtime-token",
            ),
        )

    async def get_runtime_session_history_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_workspace_id": runtime_workspace_id,
                "runtime_session_id": runtime_session_id,
            }
        )
        return RuntimeSessionHistoryControl(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            session_id=456,
            endpoint=RuntimeEndpoint(
                runtime_workspace_id=runtime_workspace_id,
                runtime_id=f"rt-{runtime_workspace_id}",
                endpoint_base_url="http://runtime.example",
                endpoint_host_header="runtime.example",
                api_key="runtime-token",
            ),
        )

    async def record_runtime_run_history_checkpoint(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        history_message_count_before_run: int,
        source_snapshot: dict | None = None,
    ):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_run_id": runtime_run_id,
                "history_message_count_before_run": history_message_count_before_run,
                "source_snapshot": source_snapshot,
            }
        )

    async def bind_runtime_provider_run(self, *, org_id: int, user_id: int, runtime_run_id: str, runtime_provider_run_id: str):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_run_id": runtime_run_id,
                "runtime_provider_run_id": runtime_provider_run_id,
            }
        )

    async def mark_runtime_run_stopped(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        provider_response: dict | None = None,
        error_message: str | None = None,
    ):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_run_id": runtime_run_id,
                "runtime_provider_run_id": runtime_provider_run_id,
                "provider_response": provider_response,
                "error_message": error_message,
            }
        )
        return CreatedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            runtime_run_id=runtime_run_id,
            model_entry_id=10,
            status="cancel_failed" if error_message else "canceling",
        )

    async def mark_runtime_run_canceled(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
    ):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_run_id": runtime_run_id,
                "runtime_provider_run_id": runtime_provider_run_id,
                "status": "canceled",
            }
        )
        return CreatedRuntimeRun(123, "rs-123-1", runtime_run_id, 10, "canceled")

    async def mark_runtime_run_finished(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        status: str,
        error_message: str | None = None,
    ):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_run_id": runtime_run_id,
                "runtime_provider_run_id": runtime_provider_run_id,
                "status": status,
                "error_message": error_message,
            }
        )
        return CreatedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            runtime_run_id=runtime_run_id,
            model_entry_id=10,
            status=status,
        )

    async def append_runtime_run_event(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        event_name: str,
        payload: dict,
    ):
        self.requests.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_run_id": runtime_run_id,
                "event_name": event_name,
                "payload": payload,
            }
        )


class FakeAssetVisibilityService:
    def __init__(self, visible_ids: set[int]) -> None:
        self.visible_ids = visible_ids
        self.calls: list[dict] = []

    async def list_visible_assets(self, *, principal: Principal, asset_type: str, asset_ids: set[int]):
        self.calls.append(
            {
                "principal": principal,
                "asset_type": asset_type,
                "asset_ids": set(asset_ids),
            }
        )
        return [
            VisibleAssetRef(
                asset_type=asset_type,
                asset_id=asset_id,
                version_id=None,
                status="enabled",
                visibility="organization",
            )
            for asset_id in sorted(asset_ids & self.visible_ids)
        ]


class SqlAlchemyRuntimeChatStoreHarness(SqlAlchemyRuntimeChatStore):
    def __init__(self, *, bound_ids: set[int], visible_ids: set[int]) -> None:
        self.fake_visibility_service = FakeAssetVisibilityService(visible_ids)
        super().__init__(db=object(), asset_visibility_service=self.fake_visibility_service)  # type: ignore[arg-type]
        self.bound_ids = bound_ids

    async def _runtime_bound_asset_ids(self, *, org_id: int, runtime_workspace_id: int, asset_type: str):
        return self.bound_ids


class EmptyPolicyResult:
    def first(self):
        return None


class EmptyRowsResult:
    def all(self):
        return []


class PolicyQueryDb:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return EmptyPolicyResult()


class StatementCaptureDb:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return EmptyRowsResult()


class FinishedRunDb:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, record) -> None:
        self.added.append(record)

    async def commit(self) -> None:
        self.commits += 1


class FakeRouteCache:
    def __init__(self) -> None:
        self.events = []

    async def invalidate(self, event) -> None:
        self.events.append(event)


class FinishedRunStoreHarness(SqlAlchemyRuntimeChatStore):
    def __init__(self, run: RuntimeRun, route_cache=None) -> None:
        self.run = run
        self.finished_db = FinishedRunDb()
        super().__init__(db=self.finished_db, route_cache=route_cache)  # type: ignore[arg-type]

    async def _runtime_run_control_row(self, **_kwargs):
        return self.run, object()


def mysql_operational_error(code: int, message: str) -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(code, message))


class RunCreateRetryDb:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class RunCreateRetryStore(SqlAlchemyRuntimeChatStore):
    def __init__(
        self,
        *,
        errors: list[OperationalError],
        sleep,
    ) -> None:
        self.retry_db = RunCreateRetryDb()
        super().__init__(
            self.retry_db,  # type: ignore[arg-type]
            run_create_retry_base_delay_seconds=0,
            sleep=sleep,
        )
        self.errors = list(errors)
        self.attempted_run_ids: list[str] = []

    async def _create_runtime_run_once(
        self,
        *,
        org_id: int,
        user_id: int,
        prepared: PreparedRuntimeRun,
        generated_runtime_run_id: str,
        timing: RuntimeChatTiming | None = None,
    ) -> CreatedRuntimeRun:
        self.attempted_run_ids.append(generated_runtime_run_id)
        if self.errors:
            raise self.errors.pop(0)
        return CreatedRuntimeRun(
            runtime_workspace_id=prepared.runtime_workspace_id,
            runtime_session_id=prepared.runtime_session_id,
            runtime_run_id=generated_runtime_run_id,
            model_entry_id=prepared.model_entry_id,
            status="accepted",
        )


class SequentialScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def one_or_none(self):
        return self.value


class NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class ActiveRunGuardDb:
    def __init__(self, values: list[object | None]) -> None:
        self.values = values
        self.statements = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return SequentialScalarResult(self.values.pop(0))

    def add(self, record) -> None:
        self.added.append(record)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    def begin_nested(self) -> NestedTransaction:
        return NestedTransaction()

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, _record) -> None:
        return None


class DuplicateRunGuardDb(ActiveRunGuardDb):
    async def flush(self) -> None:
        self.flushes += 1
        raise IntegrityError(
            "INSERT INTO runtime_run",
            {},
            Exception(
                1062,
                "Duplicate entry for key 'uq_runtime_run_runtime_id'",
            ),
        )


def ready_cloud_profile(
    *,
    default_model_id: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        profile_config={},
        default_model_id=default_model_id,
    )


def base_policy() -> RuntimeChatPolicy:
    return RuntimeChatPolicy(
        runtime_workspace_id=123,
        session_id=456,
        org_id=7,
        user_id=9,
        runtime_session_id="rs-123-1",
        session_model_entry_id=10,
        default_model_entry_id=11,
        visible_model_entry_ids={10, 11},
        visible_expert_ids={1},
        visible_skill_ids={2},
        visible_mcp_ids={3},
        visible_knowledge_base_ids={4},
        runtime_ready=True,
        reload_decision_input=ReloadDecisionInput(
            runtime_status=RuntimeStatus.READY_IDLE,
            scopes={ProjectionScope.PERSONA},
            hot_reload_supported=True,
        ),
    )


def base_intent() -> RuntimeChatRequestIntent:
    return RuntimeChatRequestIntent(
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        message="hello",
        enabled_expert_ids=[1],
        enabled_skill_ids=[2],
        enabled_mcp_ids=[3],
        enabled_knowledge_base_ids=[4],
    )


async def test_web_run_with_no_run_level_assets_uses_pruned_policy_loader_only() -> None:
    class Store(FakeRuntimeChatStore):
        def __init__(self) -> None:
            super().__init__(base_policy())
            self.web_policy_calls = 0
            self.full_policy_calls = 0

        async def get_web_chat_policy(self, **_kwargs):
            self.web_policy_calls += 1
            return replace(
                base_policy(),
                visible_expert_ids=set(),
                visible_skill_ids=set(),
                visible_mcp_ids=set(),
                visible_knowledge_base_ids=set(),
            )

        async def get_chat_policy(self, **kwargs):
            self.full_policy_calls += 1
            return await super().get_chat_policy(**kwargs)

    store = Store()
    await RuntimeChatService(store).prepare_run(
        principal=Principal(org_id=7, user_id=9),
        intent=RuntimeChatRequestIntent(123, "rs-123-1", "hello", source_type="web", channel="web"),
    )
    await RuntimeChatService(store).prepare_run(
        principal=Principal(org_id=7, user_id=9),
        intent=RuntimeChatRequestIntent(123, "rs-123-1", "scheduled", source_type="scheduled", channel="scheduled"),
    )

    assert store.web_policy_calls == 1
    assert store.full_policy_calls == 1


async def test_prepare_run_uses_session_model_and_visible_assets() -> None:
    store = FakeRuntimeChatStore(base_policy())

    result = await RuntimeChatService(store).prepare_run(
        principal=Principal(org_id=7, user_id=9),
        intent=base_intent(),
    )

    assert result.model_entry_id == 10
    assert result.runtime_workspace_id == 123
    assert result.runtime_session_id == "rs-123-1"
    assert result.enabled_expert_ids == [1]
    assert result.allow_new_run is True
    assert store.requests == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_workspace_id": 123,
            "runtime_session_id": "rs-123-1",
        }
    ]


async def test_prepare_run_allows_requested_visible_model() -> None:
    result = await RuntimeChatService(FakeRuntimeChatStore(base_policy())).prepare_run(
        principal=Principal(org_id=7, user_id=9),
        intent=replace(base_intent(), model_entry_id=11),
    )

    assert result.model_entry_id == 11


async def test_create_run_persists_prepared_runtime_run() -> None:
    store = FakeRuntimeChatStore(base_policy())
    result = await RuntimeChatService(store).create_run(
        principal=Principal(org_id=7, user_id=9),
        intent=replace(
            base_intent(),
            message="runtime attachment context\n用户问题",
            audit_message="用户问题",
            channel="web",
        ),
    )

    assert result.runtime_run_id == "rr-created"
    assert result.runtime_session_id == "rs-123-1"
    assert result.model_entry_id == 10
    assert result.status == "accepted"
    assert store.prepared_runs[0].message == "runtime attachment context\n用户问题"
    assert store.prepared_runs[0].audit_message == "用户问题"
    assert store.prepared_runs[0].channel == "web"


async def test_expert_collaboration_summary_inherits_platform_session_model_and_hides_internal_prompt() -> None:
    store = FakeRuntimeChatStore(base_policy())

    await RuntimeChatService(store).create_run(
        principal=Principal(org_id=7, user_id=9),
        intent=RuntimeChatRequestIntent(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            message="[平台内部通知：专家协作任务已经完成]",
            source_type="expert_collaboration",
            audit_message="",
            channel="expert_collaboration",
        ),
    )

    prepared = store.prepared_runs[0]
    assert prepared.model_entry_id == 10
    assert prepared.source_type == "expert_collaboration"
    assert prepared.audit_message == ""
    assert prepared.channel == "expert_collaboration"


async def test_create_run_rejects_when_selected_session_model_is_no_longer_visible() -> None:
    store = FakeRuntimeChatStore(replace(base_policy(), visible_model_entry_ids={11}))

    with pytest.raises(RuntimeChatPreparationError, match="model entry is not visible"):
        await RuntimeChatService(store).create_run(
            principal=Principal(org_id=7, user_id=9),
            intent=base_intent(),
        )

    assert store.created_runs == 0


async def test_create_run_rejects_when_fallback_default_model_is_no_longer_visible() -> None:
    policy = replace(
        base_policy(),
        session_model_entry_id=None,
        default_model_entry_id=11,
        visible_model_entry_ids=set(),
    )
    store = FakeRuntimeChatStore(policy)

    with pytest.raises(RuntimeChatPreparationError, match="model entry is not visible"):
        await RuntimeChatService(store).create_run(
            principal=Principal(org_id=7, user_id=9),
            intent=base_intent(),
        )

    assert store.created_runs == 0


async def test_create_run_records_prepare_and_profile_gate_timing() -> None:
    store = FakeRuntimeChatStore(base_policy())
    values = iter((1.0, 1.025))
    timing = RuntimeChatTiming(clock=lambda: next(values))

    result = await RuntimeChatService(store).create_run(
        principal=Principal(org_id=7, user_id=9),
        intent=base_intent(),
        timing=timing,
    )

    assert result.runtime_run_id == "rr-created"
    assert store.received_timing is timing
    assert timing.safe_snapshot(
        duration_pairs=(("run_prepare_done", "run_profile_gate_done"),)
    )["durations_ms"] == {
        "run_prepare_done_to_run_profile_gate_done": 25.0,
    }


async def test_get_run_control_uses_platform_runtime_run_id() -> None:
    store = FakeRuntimeChatStore(base_policy())

    result = await RuntimeChatService(store).get_run_control(
        principal=Principal(org_id=7, user_id=9),
        runtime_run_id="rr-123",
    )

    assert result.runtime_provider_run_id == "run-provider-1"
    assert store.requests == [{"org_id": 7, "user_id": 9, "runtime_run_id": "rr-123"}]


async def test_get_session_history_control_uses_runtime_session_owner_scope() -> None:
    store = FakeRuntimeChatStore(base_policy())

    result = await RuntimeChatService(store).get_session_history_control(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
    )

    assert result.runtime_workspace_id == 123
    assert result.runtime_session_id == "rs-123-1"


async def test_get_session_history_control_exposes_platform_message_count() -> None:
    store = FakeRuntimeChatStore(base_policy())

    result = await RuntimeChatService(store).get_session_history_control(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
    )

    assert result.message_count == 0
    assert result.endpoint.endpoint_base_url == "http://runtime.example"
    assert store.requests == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_workspace_id": 123,
            "runtime_session_id": "rs-123-1",
        }
    ]


async def test_sql_web_chat_policy_accepts_ready_profile_row() -> None:
    row = (
        SimpleNamespace(
            id=456,
            org_id=7,
            user_id=9,
            runtime_session_id="rs-123-1",
            session_model_entry_id=10,
        ),
        SimpleNamespace(default_model_id=10),
        SimpleNamespace(
            id=17,
            config_generation=7,
            hermes_profile_name="default",
        ),
        SimpleNamespace(
            status="ready",
            active_run_count=0,
        ),
    )

    class Result:
        def first(self):
            return row

    class Db:
        async def execute(self, _statement):
            return Result()

    store = SqlAlchemyRuntimeChatStore(Db())  # type: ignore[arg-type]

    async def visible_models(**_kwargs):
        return [
            SimpleNamespace(
                model_entry_id=10,
                is_default=True,
            )
        ]

    store._visible_models = visible_models  # type: ignore[method-assign]

    result = await store.get_web_chat_policy(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
    )

    assert result is not None
    assert result.runtime_agent_profile_id == 17
    assert result.hermes_profile_name == "default"


def retry_prepared_run(
    *,
    runtime_run_id: str | None = None,
) -> PreparedRuntimeRun:
    return PreparedRuntimeRun(
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        model_entry_id=10,
        message="hello",
        source_type="web",
        enabled_expert_ids=[],
        enabled_skill_ids=[],
        enabled_mcp_ids=[],
        enabled_knowledge_base_ids=[],
        allow_new_run=True,
        runtime_run_id=runtime_run_id,
    )


async def test_run_create_retries_deadlock_with_one_stable_run_id() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    store = RunCreateRetryStore(
        errors=[mysql_operational_error(1213, "deadlock")],
        sleep=sleep,
    )
    timing = RuntimeChatTiming()

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=retry_prepared_run(),
        timing=timing,
    )

    assert result.runtime_run_id.startswith("rr-123-")
    assert store.attempted_run_ids == [
        result.runtime_run_id,
        result.runtime_run_id,
    ]
    assert store.retry_db.rollbacks == 1
    assert sleeps == [0]
    assert timing.safe_snapshot()["metadata"] == {
        "run_db_attempts": "2",
        "run_db_retry_count": "1",
        "run_db_retry_error": "1213",
    }


async def test_run_create_raises_database_busy_after_deadlock_budget() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    store = RunCreateRetryStore(
        errors=[
            mysql_operational_error(1213, "deadlock 1"),
            mysql_operational_error(1213, "deadlock 2"),
            mysql_operational_error(1213, "deadlock 3"),
        ],
        sleep=sleep,
    )

    with pytest.raises(RuntimeChatDatabaseBusyError) as raised:
        await store.create_runtime_run(
            org_id=7,
            user_id=9,
            prepared=retry_prepared_run(runtime_run_id="web-stable-run"),
        )

    assert raised.value.mysql_error_code == 1213
    assert store.attempted_run_ids == ["web-stable-run"] * 3
    assert store.retry_db.rollbacks == 3
    assert sleeps == [0, 0]


async def test_run_create_rolls_back_lock_timeout_without_retrying() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    store = RunCreateRetryStore(
        errors=[mysql_operational_error(1205, "lock timeout")],
        sleep=sleep,
    )

    with pytest.raises(RuntimeChatDatabaseBusyError) as raised:
        await store.create_runtime_run(
            org_id=7,
            user_id=9,
            prepared=retry_prepared_run(),
        )

    assert raised.value.mysql_error_code == 1205
    assert len(store.attempted_run_ids) == 1
    assert store.retry_db.rollbacks == 1
    assert sleeps == []


async def test_run_create_does_not_retry_non_transient_db_error() -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    error = mysql_operational_error(1062, "duplicate key")
    store = RunCreateRetryStore(errors=[error], sleep=sleep)

    with pytest.raises(OperationalError) as raised:
        await store.create_runtime_run(
            org_id=7,
            user_id=9,
            prepared=retry_prepared_run(),
        )

    assert raised.value is error
    assert len(store.attempted_run_ids) == 1
    assert store.retry_db.rollbacks == 0
    assert sleeps == []


async def test_sql_session_history_control_exposes_profile_generation() -> None:
    row = (
        SimpleNamespace(
            id=456,
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            message_count_cache=0,
            session_model_entry_id=10,
        ),
        SimpleNamespace(default_model_id=10),
        SimpleNamespace(
            id=17,
            config_generation=7,
            applied_revision=11,
            hermes_profile_name="default",
        ),
        SimpleNamespace(
            status="ready",
            runtime_workspace_id=123,
            runtime_id="rt-123",
            config_generation=5,
            endpoint_base_url="http://runtime.example",
            endpoint_host_header="runtime.example",
            api_key_encrypted="encrypted-runtime-token",
        ),
    )

    class Result:
        def first(self):
            return row

    class Db:
        async def scalar(self, _statement):
            return "active"

        async def execute(self, _statement):
            return Result()

    store = SqlAlchemyRuntimeChatStore(
        Db(),  # type: ignore[arg-type]
        api_key_codec=SimpleNamespace(  # type: ignore[arg-type]
            decrypt=lambda _value: "runtime-token"
        ),
    )

    result = await store.get_runtime_session_history_control(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
    )

    assert result.runtime_agent_profile_id == 17
    assert result.runtime_agent_profile_generation == 11


async def test_prepare_scheduled_source_persists_snapshot_and_history_boundary() -> None:
    store = FakeRuntimeChatStore(base_policy())

    async def history_control_with_count(**_kwargs):
        return RuntimeSessionHistoryControl(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            session_id=456,
            endpoint=RuntimeEndpoint(
                runtime_workspace_id=123,
                runtime_id="rt-123",
                endpoint_base_url="http://runtime.example",
                endpoint_host_header="runtime.example",
                api_key="runtime-token",
            ),
            message_count=7,
        )

    store.get_runtime_session_history_control = history_control_with_count  # type: ignore[method-assign]

    result = await RuntimeChatService(store).prepare_scheduled_source(
        principal=Principal(org_id=7, user_id=9),
        created=CreatedRuntimeRun(123, "rs-123-1", "rr-123", 10, "running"),
        scheduled_task_id=42,
        scheduled_task_name="每日工作总结",
        trigger_kind="scheduled",
    )

    assert result == {
        "type": "scheduled",
        "scheduled_task_id": 42,
        "scheduled_task_name": "每日工作总结",
        "trigger_kind": "scheduled",
        "task_available": True,
    }
    assert store.requests[-1]["history_message_count_before_run"] == 7
    assert store.requests[-1]["source_snapshot"] == result


async def test_bind_provider_run_persists_hermes_run_mapping() -> None:
    store = FakeRuntimeChatStore(base_policy())

    await RuntimeChatService(store).bind_provider_run(
        principal=Principal(org_id=7, user_id=9),
        runtime_run_id="rr-123",
        runtime_provider_run_id="run-provider-1",
    )

    assert store.requests == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_run_id": "rr-123",
            "runtime_provider_run_id": "run-provider-1",
        }
    ]


async def test_mark_stopped_records_runtime_stop_result() -> None:
    store = FakeRuntimeChatStore(base_policy())

    result = await RuntimeChatService(store).mark_stopped(
        principal=Principal(org_id=7, user_id=9),
        runtime_run_id="rr-123",
        runtime_provider_run_id="run-provider-1",
        provider_response={"status": "stopped"},
    )

    assert result.status == "canceling"
    assert store.requests == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_run_id": "rr-123",
            "runtime_provider_run_id": "run-provider-1",
            "provider_response": {"status": "stopped"},
            "error_message": None,
        }
    ]


async def test_mark_canceled_records_provider_terminal() -> None:
    store = FakeRuntimeChatStore(base_policy())

    result = await RuntimeChatService(store).mark_canceled(
        principal=Principal(org_id=7, user_id=9),
        runtime_run_id="rr-123",
        runtime_provider_run_id="run-provider-1",
    )

    assert result.status == "canceled"
    assert store.requests == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_run_id": "rr-123",
            "runtime_provider_run_id": "run-provider-1",
            "status": "canceled",
        }
    ]


async def test_prepare_run_rejects_missing_session_policy() -> None:
    await assert_prepare_error(
        policy=None,
        intent=base_intent(),
        message="runtime session not found",
    )


async def test_prepare_run_rejects_owner_mismatch() -> None:
    await assert_prepare_error(
        policy=replace(base_policy(), user_id=10),
        intent=base_intent(),
        message="runtime session owner mismatch",
    )


async def test_prepare_run_rejects_not_ready_runtime() -> None:
    await assert_prepare_error(
        policy=replace(base_policy(), runtime_ready=False),
        intent=base_intent(),
        message="runtime is not ready",
    )


async def test_prepare_run_rejects_security_reload_until_refreshed() -> None:
    await assert_prepare_error(
        policy=replace(
            base_policy(),
            reload_decision_input=ReloadDecisionInput(
                runtime_status=RuntimeStatus.ACTIVE,
                scopes={ProjectionScope.SECURITY},
                active_run_count=1,
            ),
        ),
        intent=base_intent(),
        message="security or model policy change must apply before any new run",
    )


async def test_prepare_run_rejects_invisible_model() -> None:
    await assert_prepare_error(
        policy=base_policy(),
        intent=replace(base_intent(), model_entry_id=99),
        message="model entry is not visible",
    )


async def test_prepare_run_rejects_invisible_asset() -> None:
    await assert_prepare_error(
        policy=base_policy(),
        intent=replace(base_intent(), enabled_skill_ids=[999]),
        message="skill is not visible: 999",
    )


async def test_sql_chat_store_filters_bound_assets_through_visibility_service() -> None:
    store = SqlAlchemyRuntimeChatStoreHarness(bound_ids={1, 2, 3}, visible_ids={1, 3})

    visible_ids = await store._visible_bound_asset_ids(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=123,
        asset_type="skill",
    )

    assert visible_ids == {1, 3}
    assert store.fake_visibility_service.calls == [
        {
            "principal": Principal(org_id=7, user_id=9),
            "asset_type": "skill",
            "asset_ids": {1, 2, 3},
        }
    ]


async def test_sql_chat_store_skips_visibility_service_when_no_bound_assets() -> None:
    store = SqlAlchemyRuntimeChatStoreHarness(bound_ids=set(), visible_ids={1})

    visible_ids = await store._visible_bound_asset_ids(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=123,
        asset_type="mcp",
    )

    assert visible_ids == set()
    assert store.fake_visibility_service.calls == []


async def test_sql_chat_policy_queries_only_active_session() -> None:
    db = PolicyQueryDb()

    result = await SqlAlchemyRuntimeChatStore(db).get_chat_policy(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
    )

    assert result is None
    assert len(db.statements) == 1
    statement = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "runtime_session.status = 'active'" in statement


async def test_sql_chat_visible_models_require_active_provider_and_latest_published_catalog() -> None:
    db = StatementCaptureDb()

    visible_ids = await SqlAlchemyRuntimeChatStore(db)._visible_model_entry_ids(
        org_id=7,
        user_id=9,
    )

    assert visible_ids == set()
    assert len(db.statements) == 2
    department_statement = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    statement = str(db.statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert 'FROM "user" LEFT OUTER JOIN sys_user' in department_statement
    assert "sys_dept.status = 1" in department_statement
    assert "JOIN model_provider ON model_provider.id = model_entry.provider_id" in statement
    assert "model_entry.status = 'active'" in statement
    assert "model_provider.status = 'active'" in statement
    assert "model_upgrade_log.action = 'model_catalog_publish'" in statement
    assert "model_upgrade_log.status = 'published'" in statement
    assert "model_entry.catalog_version = (SELECT max(model_upgrade_log.catalog_version)" in statement
    assert "model_provider.catalog_version = (SELECT max(model_upgrade_log.catalog_version)" in statement


async def test_mark_finished_records_terminal_runtime_result() -> None:
    store = FakeRuntimeChatStore(base_policy())

    result = await RuntimeChatService(store).mark_finished(
        principal=Principal(org_id=7, user_id=9),
        runtime_run_id="rr-123",
        runtime_provider_run_id="run-provider-1",
        status="completed",
    )

    assert result.status == "completed"
    assert store.requests == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_run_id": "rr-123",
            "runtime_provider_run_id": "run-provider-1",
            "status": "completed",
            "error_message": None,
        }
    ]


async def test_append_event_uses_platform_principal_scope() -> None:
    store = FakeRuntimeChatStore(base_policy())

    await RuntimeChatService(store).append_event(
        principal=Principal(org_id=7, user_id=9),
        runtime_run_id="rr-123",
        event_name="chat.message.delta",
        payload={"delta": "hello"},
    )

    assert store.requests == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_run_id": "rr-123",
            "event_name": "chat.message.delta",
            "payload": {"delta": "hello"},
        }
    ]


async def test_list_events_uses_principal_scope_and_after_sequence() -> None:
    class EventReplayStore(FakeRuntimeChatStore):
        async def list_runtime_run_events(self, **kwargs):
            self.requests.append(kwargs)
            return ["event-3"]

    store = EventReplayStore(base_policy())

    events = await RuntimeChatService(store).list_events(
        principal=Principal(org_id=7, user_id=9),
        runtime_run_id="rr-123",
        after_seq=2,
    )

    assert events == ["event-3"]
    assert store.requests == [
        {"org_id": 7, "user_id": 9, "runtime_run_id": "rr-123", "after_seq": 2}
    ]


async def test_sql_chat_store_keeps_stop_request_active_until_provider_terminal() -> None:
    run = RuntimeRun(
        id=122,
        org_id=7,
        user_id=9,
        runtime_workspace_id=456,
        runtime_session_id="rs-456-1",
        runtime_run_id="rr-stop",
        status="running",
    )
    store = FinishedRunStoreHarness(run)

    requested = await store.mark_runtime_run_stopped(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-stop",
        runtime_provider_run_id="hermes-stop",
        provider_response={"status": "interrupted"},
    )

    assert requested.status == "canceling"
    assert run.status == "running"
    assert run.stop_requested_at is not None
    assert run.finished_at is None

    canceled = await store.mark_runtime_run_canceled(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-stop",
        runtime_provider_run_id="hermes-stop",
    )

    assert canceled.status == "canceled"
    assert run.status == "canceled"
    assert run.finished_at is not None
    assert store.finished_db.commits == 2


async def test_sql_chat_store_stop_failure_leaves_run_active_without_cancel_timeout() -> None:
    run = RuntimeRun(
        id=121,
        org_id=7,
        user_id=9,
        runtime_workspace_id=456,
        runtime_session_id="rs-456-1",
        runtime_run_id="rr-stop-failed",
        status="running",
    )
    store = FinishedRunStoreHarness(run)

    result = await store.mark_runtime_run_stopped(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-stop-failed",
        runtime_provider_run_id="hermes-stop-failed",
        error_message="interrupt unavailable",
    )

    assert result.status == "cancel_failed"
    assert run.status == "running"
    assert run.stop_requested_at is None
    assert run.finished_at is None


async def test_sql_chat_store_marks_finished_run_and_writes_audit() -> None:
    run = RuntimeRun(
        id=123,
        org_id=7,
        user_id=9,
        runtime_workspace_id=456,
        runtime_session_id="rs-456-1",
        runtime_run_id="rr-456-1",
        status="running",
    )
    store = FinishedRunStoreHarness(run)

    result = await store.mark_runtime_run_finished(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-456-1",
        runtime_provider_run_id="run-provider-1",
        status="completed",
    )

    assert result.status == "completed"
    assert run.status == "completed"
    assert run.finished_at is not None
    assert run.response_summary == {
        "hermes_run_id": "run-provider-1",
        "stream": {"status": "completed", "error_message": None},
    }
    assert store.finished_db.commits == 1
    assert len(store.finished_db.added) == 1
    audit = store.finished_db.added[0]
    assert audit.action == "runtime_run.finished"
    assert audit.detail == {
        "runtime_provider_run_id": "run-provider-1",
        "status": "completed",
        "error_message": None,
    }


async def test_sql_chat_store_invalidates_exact_route_when_run_finishes() -> None:
    run = RuntimeRun(
        id=123,
        org_id=7,
        user_id=9,
        runtime_workspace_id=456,
        runtime_session_id="rs-456-1",
        runtime_run_id="rr-456-1",
        status="running",
    )
    route_cache = FakeRouteCache()
    store = FinishedRunStoreHarness(run, route_cache=route_cache)

    await store.mark_runtime_run_finished(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-456-1",
        runtime_provider_run_id="run-provider-1",
        status="completed",
    )

    assert route_cache.events[0].event_type == "run_finished"
    assert route_cache.events[0].runtime_workspace_id == 456
    assert route_cache.events[0].runtime_run_id == "rr-456-1"


async def test_sql_chat_store_does_not_replace_an_existing_terminal_status() -> None:
    run = RuntimeRun(
        id=123,
        org_id=7,
        user_id=9,
        runtime_workspace_id=456,
        runtime_session_id="rs-456-1",
        runtime_run_id="rr-456-1",
        status="failed",
        error_message="runtime run reconciliation timeout",
    )
    store = FinishedRunStoreHarness(run)

    result = await store.mark_runtime_run_finished(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-456-1",
        runtime_provider_run_id="run-provider-late",
        status="completed",
    )

    assert result.status == "failed"
    assert run.status == "failed"
    assert run.error_message == "runtime run reconciliation timeout"
    assert store.finished_db.added == []
    assert store.finished_db.commits == 0


async def test_sql_chat_store_rejects_second_active_run_in_same_session() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    active_run = RuntimeRun(
        id=99,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        session_id=456,
        runtime_session_id="rs-123-1",
        runtime_run_id="rr-active",
        status="running",
    )
    db = ActiveRunGuardDb([
        SimpleNamespace(status="ready"),
        ready_cloud_profile(),
        session,
        active_run,
    ])
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    with pytest.raises(RuntimeChatActiveRunError):
        await store.create_runtime_run(
            org_id=7,
            user_id=9,
            prepared=PreparedRuntimeRun(
                runtime_workspace_id=123,
                runtime_session_id="rs-123-1",
                model_entry_id=10,
                message="hello",
                source_type="web",
                enabled_expert_ids=[],
                enabled_skill_ids=[],
                enabled_mcp_ids=[],
                enabled_knowledge_base_ids=[],
                allow_new_run=True,
            ),
        )

    assert db.added == []
    assert db.commits == 0
    statements = [str(statement.compile(compile_kwargs={"literal_binds": True})) for statement in db.statements]
    assert "hermes_runtime_binding" in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "cloud_hermes_profile" in statements[1]
    assert "runtime_session" in statements[2]
    assert "FOR UPDATE" in statements[2]
    assert "runtime_run.status IN ('accepted', 'pending', 'running')" in statements[3]


async def test_sql_chat_store_creates_scheduled_run_as_queued_without_mysql_busy_gate() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    source = {
        "type": "scheduled",
        "scheduled_task_id": 7,
        "scheduled_task_name": "daily",
        "trigger_kind": "scheduled",
        "task_available": True,
    }
    db = ActiveRunGuardDb([
        None,
        SimpleNamespace(status="ready"),
        ready_cloud_profile(),
        session,
    ])
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="scheduled prompt",
            source_type="scheduled",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
            runtime_run_id="rr-scheduled-0001",
            source_snapshot=source,
        ),
    )

    assert result.status == "queued"
    assert len(db.statements) == 4  # reserved-id precheck + binding/profile/session
    created = db.added[0]
    assert created.status == "queued"
    assert created.started_at is None
    assert created.request_payload["source"] == source


async def test_sql_chat_store_reuses_reserved_runtime_run_id() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    existing = RuntimeRun(
        id=99,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        session_id=456,
        runtime_session_id="rs-123-1",
        runtime_run_id="rr-im-wecom-101",
        model_entry_id=10,
        status="running",
        source_type="wecom",
    )
    db = ActiveRunGuardDb(
        [
            (existing, session),
        ]
    )
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]
    values = iter([1.0])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="wecom",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
            runtime_run_id="rr-im-wecom-101",
        ),
        timing=timing,
    )

    assert result.runtime_run_id == "rr-im-wecom-101"
    assert result.reused is True
    assert result.status == "running"
    assert db.added == []
    assert db.commits == 0
    assert db.rollbacks == 1
    assert len(db.statements) == 1
    assert "runtime_run.runtime_run_id" in str(db.statements[0])
    assert timing.safe_snapshot()["durations_ms"] == {}


async def test_sql_chat_store_reuses_same_active_run_after_precheck_race() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    existing = RuntimeRun(
        id=99,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        session_id=456,
        runtime_session_id="rs-123-1",
        runtime_run_id="rr-im-wecom-101",
        model_entry_id=10,
        status="running",
        source_type="wecom",
    )
    db = ActiveRunGuardDb(
        [
            None,
            SimpleNamespace(status="ready"),
            ready_cloud_profile(),
            session,
            existing,
        ]
    )
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="wecom",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
            runtime_run_id="rr-im-wecom-101",
        ),
    )

    assert result.reused is True
    assert result.runtime_run_id == "rr-im-wecom-101"
    assert db.commits == 0
    assert db.rollbacks == 1
    assert db.added == []
    assert len(db.statements) == 5


async def test_sql_chat_store_recovers_narrow_runtime_run_unique_race() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    existing = RuntimeRun(
        id=99,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        session_id=456,
        runtime_session_id="rs-123-1",
        runtime_run_id="rr-im-wecom-101",
        model_entry_id=10,
        status="running",
        source_type="wecom",
    )
    db = DuplicateRunGuardDb(
        [
            None,
            SimpleNamespace(status="ready"),
            ready_cloud_profile(),
            session,
            None,
            (existing, session),
        ]
    )
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="wecom",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
            runtime_run_id="rr-im-wecom-101",
        ),
    )

    assert result.reused is True
    assert result.runtime_run_id == "rr-im-wecom-101"
    assert db.flushes == 1
    assert db.rollbacks == 1
    assert db.commits == 0
    assert session.last_active_at is None
    assert "FOR UPDATE" in str(db.statements[-1])


async def test_sql_chat_store_rejects_prelocked_idempotency_conflict() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    existing = RuntimeRun(
        id=99,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        session_id=456,
        runtime_session_id="rs-123-1",
        runtime_run_id="rr-im-wecom-101",
        model_entry_id=10,
        status="running",
        source_type="web",
    )
    db = ActiveRunGuardDb([(existing, session)])
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    with pytest.raises(RuntimeChatIdempotencyConflictError):
        await store.create_runtime_run(
            org_id=7,
            user_id=9,
            prepared=PreparedRuntimeRun(
                runtime_workspace_id=123,
                runtime_session_id="rs-123-1",
                model_entry_id=10,
                message="hello",
                source_type="wecom",
                enabled_expert_ids=[],
                enabled_skill_ids=[],
                enabled_mcp_ids=[],
                enabled_knowledge_base_ids=[],
                allow_new_run=True,
                runtime_run_id="rr-im-wecom-101",
            ),
        )

    assert db.commits == 0
    assert db.added == []
    assert len(db.statements) == 1


async def test_sql_chat_store_creates_reserved_runtime_run_id_once() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    db = ActiveRunGuardDb(
        [
            None,
            SimpleNamespace(status="ready"),
            ready_cloud_profile(),
            session,
            None,
        ]
    )
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="wecom",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
            runtime_run_id="rr-im-wecom-101",
        ),
    )

    assert result.runtime_run_id == "rr-im-wecom-101"
    assert result.reused is False
    assert db.added[0].runtime_run_id == "rr-im-wecom-101"
    assert db.added[0].status == "pending"
    assert db.added[0].started_at is None
    assert db.commits == 1
    assert db.flushes == 1


async def test_sql_chat_store_records_run_creation_timing_without_extra_db_work() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    db = ActiveRunGuardDb(
        [
            SimpleNamespace(status="ready"),
            ready_cloud_profile(),
            session,
            None,
        ]
    )
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]
    values = iter(
        [1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08]
    )
    timing = RuntimeChatTiming(clock=lambda: next(values))

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="web",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
        ),
        timing=timing,
    )

    assert result.runtime_run_id.startswith("rr-123-")
    assert len(db.statements) == 4
    assert len(db.added) == 1
    assert db.commits == 1
    assert timing.safe_snapshot(
        duration_pairs=(
            ("run_binding_lock_done", "run_session_lock_done"),
            (
                "run_session_lock_done",
                "run_reserved_id_check_done",
            ),
            (
                "run_reserved_id_check_done",
                "run_active_check_done",
            ),
            ("run_active_check_done", "run_insert_begin"),
            ("run_insert_begin", "run_insert_done"),
            ("run_insert_done", "run_commit_begin"),
            ("run_commit_begin", "run_commit_done"),
        )
    )["durations_ms"] == {
        "run_binding_lock_done_to_run_session_lock_done": 10.0,
        "run_session_lock_done_to_run_reserved_id_check_done": 10.0,
        "run_reserved_id_check_done_to_run_active_check_done": 10.0,
        "run_active_check_done_to_run_insert_begin": 10.0,
        "run_insert_begin_to_run_insert_done": 10.0,
        "run_insert_done_to_run_commit_begin": 10.0,
        "run_commit_begin_to_run_commit_done": 10.0,
    }


async def test_sql_chat_store_ignores_timing_failure_after_commit() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        status="active",
    )
    db = ActiveRunGuardDb(
        [
            SimpleNamespace(status="ready"),
            ready_cloud_profile(),
            session,
            None,
        ]
    )
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    clock_calls = 0

    def failing_clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 9:
            raise RuntimeError("timing unavailable after commit")
        return float(clock_calls)

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="web",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
        ),
        timing=RuntimeChatTiming(clock=failing_clock),
    )

    assert result.runtime_run_id.startswith("rr-123-")
    assert len(db.statements) == 4
    assert len(db.added) == 1
    assert db.commits == 1
    assert clock_calls == 9


async def test_sql_chat_store_does_not_invalidate_absent_route_when_run_is_created() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        message_count_cache=5,
        status="active",
    )
    db = ActiveRunGuardDb([
        SimpleNamespace(status="ready"),
        ready_cloud_profile(),
        session,
        None,
    ])
    route_cache = FakeRouteCache()
    store = SqlAlchemyRuntimeChatStore(db, route_cache=route_cache)  # type: ignore[arg-type]

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="web",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
        ),
    )

    assert result.runtime_run_id.startswith("rr-123-")
    assert result.session_materialized is False
    assert route_cache.events == []


async def test_sql_chat_store_uses_hermes_materialization_marker_not_message_count() -> None:
    session = RuntimeSession(
        id=456,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        message_count_cache=0,
        hermes_materialized_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        status="active",
    )
    db = ActiveRunGuardDb([
        SimpleNamespace(status="ready"),
        ready_cloud_profile(),
        session,
        None,
    ])
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    result = await store.create_runtime_run(
        org_id=7,
        user_id=9,
        prepared=PreparedRuntimeRun(
            runtime_workspace_id=123,
            runtime_session_id="rs-123-1",
            model_entry_id=10,
            message="hello",
            source_type="web",
            enabled_expert_ids=[],
            enabled_skill_ids=[],
            enabled_mcp_ids=[],
            enabled_knowledge_base_ids=[],
            allow_new_run=True,
        ),
    )

    assert result.session_materialized is True


async def test_sql_chat_store_refuses_run_after_binding_admission_closes() -> None:
    db = ActiveRunGuardDb([SimpleNamespace(status="starting")])
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    with pytest.raises(RuntimeChatAdmissionError) as error:
        await store.create_runtime_run(
            org_id=7,
            user_id=9,
            prepared=PreparedRuntimeRun(
                runtime_workspace_id=123,
                runtime_session_id="rs-123-1",
                model_entry_id=10,
                message="hello",
                source_type="web",
                enabled_expert_ids=[],
                enabled_skill_ids=[],
                enabled_mcp_ids=[],
                enabled_knowledge_base_ids=[],
                allow_new_run=True,
            ),
        )

    assert error.value.decision.code == "RUNTIME_STARTING"
    assert db.added == []
    assert db.commits == 0
    assert len(db.statements) == 1


async def assert_prepare_error(
    *,
    policy: RuntimeChatPolicy | None,
    intent: RuntimeChatRequestIntent,
    message: str,
) -> None:
    try:
        await RuntimeChatService(FakeRuntimeChatStore(policy)).prepare_run(
            principal=Principal(org_id=7, user_id=9),
            intent=intent,
        )
    except RuntimeChatPreparationError as exc:
        assert str(exc) == message
    else:
        raise AssertionError("expected RuntimeChatPreparationError")
