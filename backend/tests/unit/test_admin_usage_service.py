from __future__ import annotations

import json

import pytest

from app.services.admin_auth import AdminPrincipal
from app.services.admin_usage import AdminUsageService, InMemoryAdminUsageStore


@pytest.mark.asyncio
async def test_admin_usage_service_returns_observability_chain_summary() -> None:
    service = AdminUsageService(InMemoryAdminUsageStore())

    summary = await service.summary(AdminPrincipal(org_id=1, user_id=1, roles=["admin"]))

    assert summary.overview.run_count == 5
    assert summary.overview.active_run_count == 1
    assert summary.overview.model_call_count == 3
    assert summary.overview.total_tokens == 89
    assert summary.runtime_workspaces[0].runtime_workspace_id == 1
    assert summary.runtime_workspaces[0].total_tokens == 89
    assert summary.models[0].model_display_name == "GPT-4.1 Mini"
    assert summary.recent_errors[0].source == "model_relay"


@pytest.mark.asyncio
async def test_admin_usage_service_does_not_surface_sensitive_fields() -> None:
    service = AdminUsageService(InMemoryAdminUsageStore())

    summary = await service.summary(AdminPrincipal(org_id=1, user_id=1, roles=["admin"]))
    serialized = json.dumps(summary, default=lambda value: getattr(value, "__dict__", str(value))).lower()

    assert "api_key" not in serialized
    assert "provider_key" not in serialized
    assert "runtime_token" not in serialized
    assert "kubeconfig" not in serialized
    assert "host_path" not in serialized
