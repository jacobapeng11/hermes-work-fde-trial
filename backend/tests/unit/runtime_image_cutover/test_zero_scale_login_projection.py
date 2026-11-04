from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import app.api.v1.user.runtime_workspace as runtime_workspace_api
import app.services.runtime_reload as runtime_reload_module
from app.services.runtime_workspace import (
    Principal,
    RuntimeBindingEnsureResult,
    RuntimeWorkspaceRecord,
    RuntimeWorkspaceService,
)


@pytest.mark.asyncio
async def test_real_bootstrap_requeues_pending_projections_after_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = Principal(org_id=1, user_id=2)
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=191,
        cloud_profile_id=191,
        org_id=1,
        user_id=2,
        user_name="测试用户",
        user_code="191",
        name="默认数字人",
        status="active",
        runtime_status="ready",
        pending_reload=True,
    )
    plan = replace(
        RuntimeWorkspaceService.build_default_binding_plan(record),
        binding_id="191",
    )
    binding = RuntimeBindingEnsureResult(
        plan=plan,
        api_server_key="runtime-key",
        reconcile_owned=False,
        binding_status="ready",
    )
    calls: list[int] = []
    commits = 0

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            nonlocal commits
            commits += 1

    class FakeService:
        async def ensure_default_for_user(self, received_principal):
            assert received_principal == principal
            return record

    class FakeWarmupRunner:
        async def warmup_bound(self, received_record, received_binding):
            assert received_record is record
            assert received_binding is binding
            return replace(record, pending_reload=False)

    class FakeReloadService:
        def __init__(self, _session):
            pass

        async def requeue_personal_projections(self, *, binding_id):
            calls.append(binding_id)

    monkeypatch.setattr(runtime_workspace_api, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        runtime_workspace_api,
        "build_runtime_infrastructure",
        lambda _session: SimpleNamespace(
            workspace_service=FakeService(),
            warmup_runner=FakeWarmupRunner(),
        ),
    )
    monkeypatch.setattr(
        runtime_reload_module,
        "RuntimeReloadService",
        FakeReloadService,
    )
    monkeypatch.setattr(runtime_workspace_api, "_bootstrap_inflight", {191})

    await runtime_workspace_api._run_real_runtime_bootstrap(
        principal,
        191,
        binding,
    )

    assert calls == [191]
    assert commits == 1
    assert runtime_workspace_api._bootstrap_inflight == set()
