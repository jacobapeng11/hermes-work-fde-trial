from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

import app.api.v1.user.runtime_workspace as runtime_workspace_api

from app.api.v1.user.runtime_workspace import (
    bootstrap_runtime_workspace,
    keepalive_runtime_workspace,
    warmup_runtime_workspace,
)
from app.main import app
from app.config import settings
from app.services.runtime_workspace import (
    Principal,
    RuntimeBindingEnsureResult,
    RuntimeWorkspaceRecord,
    RuntimeWorkspaceService,
)
from app.services.runtime_admission import RuntimeAdmissionError


def _binding_result(
    record: RuntimeWorkspaceRecord,
    *,
    created: bool,
    binding_status: str,
    initial_bootstrap_resume_eligible: bool = False,
) -> RuntimeBindingEnsureResult:
    return RuntimeBindingEnsureResult(
        plan=RuntimeWorkspaceService.build_default_binding_plan(record),
        api_server_key="runtime-key",
        created=created,
        reconcile_required=True,
        binding_status=binding_status,
        initial_bootstrap_resume_eligible=initial_bootstrap_resume_eligible,
    )


def test_runtime_workspace_api_returns_platform_owner_shape() -> None:
    client = TestClient(app)

    response = client.get("/api/v1/user/runtime-workspace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime_workspace_id"] == 1
    assert payload["cloud_profile_id"] == 1
    assert payload["org_id"] == 1
    assert payload["user_id"] == 1
    assert "default_model_id" in payload
    assert "hrm_user_id" not in payload
    assert "sso_subject" not in payload
    assert "workspace_id" not in payload


def test_runtime_workspace_warmup_api_invokes_warmup_contract() -> None:
    client = TestClient(app)

    response = client.post("/api/v1/user/runtime-workspace/warmup")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime_workspace_id"] == 1
    assert payload["runtime_status"] == "ready"
    assert "api_key" not in payload
    assert "api_key_encrypted" not in payload


async def test_runtime_workspace_keepalive_api_returns_no_runtime_details() -> None:
    class FakeService:
        called = False

        async def keepalive_default_for_user(self, principal) -> None:
            assert principal == Principal(org_id=1, user_id=1)
            self.called = True

    service = FakeService()

    response = await keepalive_runtime_workspace(
        principal=Principal(org_id=1, user_id=1),
        service=service,
    )

    assert response.status_code == 204
    assert response.body == b""
    assert service.called is True


@pytest.mark.parametrize(
    "runtime_status",
    [
        "force_stopped",
        "force_stop_requested",
        "force_stop_failed",
        "restart_requested",
    ],
)
@pytest.mark.asyncio
async def test_user_cannot_warm_up_or_bootstrap_admin_controlled_runtime(
    runtime_status: str,
) -> None:
    class ControlledRuntimeService:
        async def ensure_default_for_user(self, principal):
            return RuntimeWorkspaceRecord(
                runtime_workspace_id=5,
                cloud_profile_id=5,
                org_id=principal.org_id,
                user_id=principal.user_id,
                user_name="测试用户",
                user_code="employee-1",
                name="默认数字人",
                status="active",
                runtime_status=runtime_status,
            )

    service = ControlledRuntimeService()
    principal = Principal(org_id=1, user_id=1)
    with pytest.raises(HTTPException) as warmup_error:
        await warmup_runtime_workspace(
            principal=principal,
            service=service,
            warmup_runner=object(),
        )
    with pytest.raises(HTTPException) as bootstrap_error:
        await bootstrap_runtime_workspace(
            background_tasks=object(),
            principal=principal,
            service=service,
            db=object(),
        )

    assert getattr(warmup_error.value, "status_code", None) == 409
    assert getattr(bootstrap_error.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_contract_bootstrap_keeps_synchronous_first_runtime_warmup(monkeypatch) -> None:
    calls: list[str] = []

    class ContractService:
        async def ensure_default_for_user(self, principal):
            return RuntimeWorkspaceRecord(
                runtime_workspace_id=118,
                cloud_profile_id=118,
                org_id=principal.org_id,
                user_id=principal.user_id,
                user_name="测试用户",
                user_code="employee-118",
                name="默认数字人",
                status="active",
                runtime_status=None,
            )

        async def warmup_default_for_user(self, principal, _runner):
            calls.append(f"warmup:{principal.user_id}")
            return RuntimeWorkspaceRecord(
                runtime_workspace_id=118,
                cloud_profile_id=118,
                org_id=principal.org_id,
                user_id=principal.user_id,
                user_name="测试用户",
                user_code="employee-118",
                name="默认数字人",
                status="active",
                runtime_status="ready",
            )

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "contract")
    tasks = BackgroundTasks()

    response = await bootstrap_runtime_workspace(
        background_tasks=tasks,
        principal=Principal(org_id=1, user_id=118),
        service=ContractService(),
        db=object(),
    )

    assert response.runtime_status == "ready"
    assert calls == ["warmup:118"]
    assert tasks.tasks == []


@pytest.mark.asyncio
async def test_first_real_bootstrap_without_binding_schedules_warmup(monkeypatch) -> None:
    calls: list[str] = []

    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=120,
        cloud_profile_id=120,
        org_id=1,
        user_id=120,
        user_name="首次用户",
        user_code="employee-120",
        name="默认数字人",
        status="active",
        runtime_status=None,
    )
    created_binding = _binding_result(
        record,
        created=True,
        binding_status="starting",
    )

    class FirstBindingStore:
        async def ensure_binding(self, record):
            assert record.runtime_workspace_id == 120
            calls.append("ensure-binding")
            return created_binding

        async def mark_bootstrap_starting(self, runtime_workspace_id):
            raise AssertionError(
                f"bootstrap must not overwrite binding status: {runtime_workspace_id}"
            )

    class FirstVisitService:
        store = FirstBindingStore()

        async def ensure_default_for_user(self, principal):
            assert principal == Principal(org_id=1, user_id=120)
            if calls:
                return replace(record, runtime_status="starting")
            return record

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", set())
    tasks = BackgroundTasks()

    response = await bootstrap_runtime_workspace(
        background_tasks=tasks,
        principal=Principal(org_id=1, user_id=120),
        service=FirstVisitService(),
        db=object(),
    )

    assert response.runtime_status == "starting"
    assert calls == ["ensure-binding"]
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].func == runtime_workspace_api._run_real_runtime_bootstrap
    assert tasks.tasks[0].args == (
        Principal(org_id=1, user_id=120),
        120,
        created_binding,
    )


@pytest.mark.asyncio
async def test_ready_real_bootstrap_schedules_zero_scale_activation(
    monkeypatch,
) -> None:
    principal = Principal(org_id=1, user_id=128)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=128,
        cloud_profile_id=128,
        org_id=1,
        user_id=128,
        user_name="缩零用户",
        user_code="employee-128",
        name="默认数字人",
        status="active",
        runtime_status="ready",
        pending_reload=False,
    )
    ready_binding = _binding_result(
        record,
        created=False,
        binding_status="ready",
    )
    ready_binding = replace(ready_binding, reconcile_required=False)

    class ReadyStore:
        async def ensure_binding(self, received_record):
            assert received_record == record
            return ready_binding

    class ReadyService:
        store = ReadyStore()

        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", set())
    tasks = BackgroundTasks()

    response = await bootstrap_runtime_workspace(
        background_tasks=tasks,
        principal=principal,
        service=ReadyService(),
        db=object(),
    )

    assert response.runtime_status == "ready"
    assert response.pending_reload is False
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].func == runtime_workspace_api._run_real_runtime_bootstrap
    assert tasks.tasks[0].args == (principal, 128, ready_binding)
    assert runtime_workspace_api._bootstrap_inflight == {128}


@pytest.mark.asyncio
async def test_failed_real_bootstrap_preserves_cas_source_and_returns_pending_summary(
    monkeypatch,
) -> None:
    principal = Principal(org_id=1, user_id=123)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=123,
        cloud_profile_id=123,
        org_id=1,
        user_id=123,
        user_name="重试用户",
        user_code="employee-123",
        name="默认数字人",
        status="active",
        runtime_status="failed",
        pending_reload=True,
    )
    failed_binding = _binding_result(
        record,
        created=False,
        binding_status="failed",
    )

    class FailedBindingStore:
        async def ensure_binding(self, received_record):
            assert received_record == record
            return failed_binding

        async def mark_bootstrap_starting(self, runtime_workspace_id):
            raise AssertionError(
                f"failed CAS source must not be overwritten: {runtime_workspace_id}"
            )

    class FailedService:
        store = FailedBindingStore()

        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", set())
    tasks = BackgroundTasks()

    response = await bootstrap_runtime_workspace(
        background_tasks=tasks,
        principal=principal,
        service=FailedService(),
        db=object(),
    )

    assert response.runtime_status == "starting"
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].args == (principal, 123, failed_binding)


@pytest.mark.asyncio
async def test_starting_initial_binding_is_explicitly_owned_only_after_slot_acquisition(
    monkeypatch,
) -> None:
    principal = Principal(org_id=1, user_id=124)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=124,
        cloud_profile_id=124,
        org_id=1,
        user_id=124,
        user_name="恢复用户",
        user_code="employee-124",
        name="默认数字人",
        status="active",
        runtime_status="starting",
        pending_reload=True,
    )
    resumable = _binding_result(
        record,
        created=False,
        binding_status="starting",
        initial_bootstrap_resume_eligible=True,
    )

    class ResumableStore:
        async def ensure_binding(self, received_record):
            assert received_record == record
            return resumable

    class ResumableService:
        store = ResumableStore()

        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", set())
    tasks = BackgroundTasks()

    response = await bootstrap_runtime_workspace(
        background_tasks=tasks,
        principal=principal,
        service=ResumableService(),
        db=object(),
    )

    handed_off = tasks.tasks[0].args[2]
    assert response.runtime_status == "starting"
    assert handed_off.created is False
    assert handed_off.reconcile_owned is True


@pytest.mark.asyncio
async def test_non_resumable_starting_binding_remains_fail_closed(monkeypatch) -> None:
    principal = Principal(org_id=1, user_id=127)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=127,
        cloud_profile_id=127,
        org_id=1,
        user_id=127,
        user_name="受控用户",
        user_code="employee-127",
        name="默认数字人",
        status="active",
        runtime_status="starting",
        pending_reload=True,
    )
    unowned = _binding_result(
        record,
        created=False,
        binding_status="starting",
        initial_bootstrap_resume_eligible=False,
    )

    class UnownedStore:
        async def ensure_binding(self, received_record):
            assert received_record == record
            return unowned

    class UnownedService:
        store = UnownedStore()

        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", set())
    tasks = BackgroundTasks()

    response = await bootstrap_runtime_workspace(
        background_tasks=tasks,
        principal=principal,
        service=UnownedService(),
        db=object(),
    )

    assert response.runtime_status == "starting"
    assert tasks.tasks == []
    assert runtime_workspace_api._bootstrap_inflight == set()


@pytest.mark.asyncio
async def test_duplicate_bootstrap_does_not_prepare_a_second_binding(monkeypatch) -> None:
    principal = Principal(org_id=1, user_id=125)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=125,
        cloud_profile_id=125,
        org_id=1,
        user_id=125,
        user_name="并发用户",
        user_code="employee-125",
        name="默认数字人",
        status="active",
        runtime_status="starting",
    )

    class ForbiddenStore:
        async def ensure_binding(self, _record):
            raise AssertionError("inflight bootstrap must own binding preparation")

    class InflightService:
        store = ForbiddenStore()

        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", {125})
    tasks = BackgroundTasks()

    response = await bootstrap_runtime_workspace(
        background_tasks=tasks,
        principal=principal,
        service=InflightService(),
        db=object(),
    )

    assert response.runtime_status == "starting"
    assert tasks.tasks == []
    assert runtime_workspace_api._bootstrap_inflight == {125}


@pytest.mark.asyncio
async def test_bootstrap_preparation_failure_releases_inflight_slot(monkeypatch) -> None:
    principal = Principal(org_id=1, user_id=126)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=126,
        cloud_profile_id=126,
        org_id=1,
        user_id=126,
        user_name="失败用户",
        user_code="employee-126",
        name="默认数字人",
        status="active",
        runtime_status=None,
    )

    class FailingStore:
        async def ensure_binding(self, _record):
            raise RuntimeError("binding preparation failed")

    class FailingService:
        store = FailingStore()

        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", set())

    with pytest.raises(RuntimeError, match="binding preparation failed"):
        await bootstrap_runtime_workspace(
            background_tasks=BackgroundTasks(),
            principal=principal,
            service=FailingService(),
            db=object(),
        )

    assert runtime_workspace_api._bootstrap_inflight == set()


@pytest.mark.asyncio
async def test_real_bootstrap_warms_first_binding(monkeypatch) -> None:
    principal = Principal(org_id=1, user_id=121)
    calls: list[str] = []
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=121,
        cloud_profile_id=121,
        org_id=1,
        user_id=121,
        user_name="首次用户",
        user_code="employee-121",
        name="默认数字人",
        status="active",
        runtime_status="starting",
    )
    binding = _binding_result(
        record,
        created=True,
        binding_status="starting",
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FirstVisitService:
        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    class FirstVisitWarmupRunner:
        async def warmup_bound(self, received_record, received_binding):
            assert received_record == record
            assert received_binding is binding
            calls.append("warmup")
            return replace(
                record,
                runtime_status="ready",
                pending_reload=False,
            )

    monkeypatch.setattr(runtime_workspace_api, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        runtime_workspace_api,
        "build_runtime_infrastructure",
        lambda _session: SimpleNamespace(
            workspace_service=FirstVisitService(),
            warmup_runner=FirstVisitWarmupRunner(),
        ),
    )
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", {121})

    await runtime_workspace_api._run_real_runtime_bootstrap(principal, 121, binding)

    assert calls == ["warmup"]
    assert runtime_workspace_api._bootstrap_inflight == set()


@pytest.mark.asyncio
async def test_real_bootstrap_activates_steady_zero_scale_runtime(
    monkeypatch,
) -> None:
    principal = Principal(org_id=1, user_id=129)
    calls: list[str] = []
    endpoint = SimpleNamespace(endpoint_base_url="http://runtime.invalid")
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=129,
        cloud_profile_id=129,
        org_id=1,
        user_id=129,
        user_name="稳态缩零用户",
        user_code="employee-129",
        name="默认数字人",
        status="active",
        runtime_status="ready",
        pending_reload=False,
    )
    binding = _binding_result(
        record,
        created=False,
        binding_status="ready",
    )
    binding = replace(binding, reconcile_required=False)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class SteadyStore:
        async def get_runtime_endpoint(self, received_record):
            assert received_record == record
            return endpoint

    class SteadyService:
        store = SteadyStore()

        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    class SteadyWarmupRunner:
        async def warmup_bound(self, received_record, received_binding):
            assert received_record == record
            assert received_binding is binding
            calls.append("warmup")
            return record

    class SteadyActivator:
        async def activate_dashboard(self, received_endpoint):
            assert received_endpoint is endpoint
            calls.append("activate")

    monkeypatch.setattr(runtime_workspace_api, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        runtime_workspace_api,
        "build_runtime_infrastructure",
        lambda _session: SimpleNamespace(
            workspace_service=SteadyService(),
            workspace_store=SteadyStore(),
            warmup_runner=SteadyWarmupRunner(),
            activator=SteadyActivator(),
        ),
    )
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", {129})

    await runtime_workspace_api._run_real_runtime_bootstrap(principal, 129, binding)

    assert calls == ["warmup", "activate"]
    assert runtime_workspace_api._bootstrap_inflight == set()


@pytest.mark.asyncio
async def test_steady_zero_scale_activation_failure_preserves_ready_binding(
    monkeypatch,
) -> None:
    principal = Principal(org_id=1, user_id=130)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=130,
        cloud_profile_id=130,
        org_id=1,
        user_id=130,
        user_name="冻结缩零用户",
        user_code="employee-130",
        name="默认数字人",
        status="active",
        runtime_status="ready",
        pending_reload=False,
    )
    binding = _binding_result(
        record,
        created=False,
        binding_status="ready",
    )
    binding = replace(binding, reconcile_required=False)

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FrozenService:
        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    class FrozenWarmupRunner:
        async def warmup_bound(self, *_args):
            raise RuntimeAdmissionError.reconcile_in_progress()

    class ForbiddenStatusStore:
        async def update_binding_status(self, *_args):
            raise AssertionError("steady login activation must not fail a ready binding")

    monkeypatch.setattr(runtime_workspace_api, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        runtime_workspace_api,
        "SqlAlchemyRuntimeBindingStatusStore",
        ForbiddenStatusStore,
    )
    monkeypatch.setattr(
        runtime_workspace_api,
        "build_runtime_infrastructure",
        lambda _session: SimpleNamespace(
            workspace_service=FrozenService(),
            warmup_runner=FrozenWarmupRunner(),
        ),
    )
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", {130})

    await runtime_workspace_api._run_real_runtime_bootstrap(principal, 130, binding)

    assert runtime_workspace_api._bootstrap_inflight == set()


@pytest.mark.asyncio
async def test_real_bootstrap_clears_inflight_when_warmup_returns_failed(
    monkeypatch,
) -> None:
    principal = Principal(org_id=1, user_id=122)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=122,
        cloud_profile_id=122,
        org_id=1,
        user_id=122,
        user_name="首次用户",
        user_code="employee-122",
        name="默认数字人",
        status="active",
        runtime_status="starting",
    )
    binding = _binding_result(
        record,
        created=True,
        binding_status="starting",
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FailedService:
        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    class FailedWarmupRunner:
        async def warmup_bound(self, received_record, received_binding):
            assert received_record == record
            assert received_binding is binding
            return replace(
                record,
                runtime_status="failed",
                pending_reload=True,
            )

    monkeypatch.setattr(runtime_workspace_api, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        runtime_workspace_api,
        "build_runtime_infrastructure",
        lambda _session: SimpleNamespace(
            workspace_service=FailedService(),
            warmup_runner=FailedWarmupRunner(),
        ),
    )
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", {122})

    await runtime_workspace_api._run_real_runtime_bootstrap(principal, 122, binding)

    assert runtime_workspace_api._bootstrap_inflight == set()
