from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from app.api.v1.admin.resources import _resource_summary, list_runtime_workspace_resources
from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.services.admin_resources import AdminRuntimeWorkspaceResourceFilter, InMemoryAdminResourceViewStore
from app.services.admin_auth import AdminPrincipal
from app.services.runtime_workspace import Principal


def test_admin_resource_view_returns_runtime_workspace_business_chain(
    admin_api_access,
) -> None:
    client = TestClient(app)
    response = client.get("/api/v1/admin/resources/runtime-workspaces")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["runtime_workspace_id"] == 1
    assert payload[0]["resource_key"] == "personal:1"
    assert payload[0]["runtime_kind"] == "personal"
    assert payload[0]["employee_no"] == "dev-user"
    assert payload[0]["runtime_name"] == "默认数字人"
    assert payload[0]["runtime_status"] == "ready"
    assert payload[0]["session_count"] == 2
    assert payload[0]["run_count"] == 5
    assert payload[0]["installed_expert_count"] == 1
    assert payload[0]["installed_skill_count"] == 2
    assert payload[0]["installed_mcp_count"] == 1
    assert payload[0]["installed_knowledge_base_count"] == 1
    assert payload[0]["default_model_display_name"] == "GPT-4.1 Mini"
    assert payload[0]["has_runtime_binding"] is True

    serialized = response.text.lower()
    assert "api_key" not in serialized
    assert "api_key_encrypted" not in serialized
    assert "provider_key" not in serialized
    assert "runtime_token" not in serialized
    assert "host_path" not in serialized
    assert "kubeconfig" not in serialized


@pytest.mark.asyncio
async def test_admin_resource_summary_maps_employee_no_without_extra_identity_fields() -> None:
    record = (await InMemoryAdminResourceViewStore().list_runtime_workspaces(org_id=1, filters=AdminRuntimeWorkspaceResourceFilter()))[0]

    summary = _resource_summary(record)

    assert summary.employee_no == "dev-user"
    assert not hasattr(summary, "sso_subject")


class CapturingResourceService:
    def __init__(self) -> None:
        self.filters = None

    async def list_runtime_workspaces(self, principal, filters=None):
        self.filters = filters
        return await InMemoryAdminResourceViewStore().list_runtime_workspaces(org_id=principal.org_id, filters=filters)


@pytest.mark.asyncio
async def test_admin_resource_route_passes_trimmed_filters_to_service() -> None:
    service = CapturingResourceService()

    await list_runtime_workspace_resources(
        employee_no=" dev-user ",
        user_name=" Admin ",
        runtime_status=" ready ",
        principal=AdminPrincipal(org_id=1, user_id=1, roles=("admin",)),
        service=service,
    )

    assert service.filters.employee_no == "dev-user"
    assert service.filters.user_name == "Admin"
    assert service.filters.runtime_status == "ready"


def test_admin_resource_view_requires_admin_role() -> None:
    async def ordinary_user() -> Principal:
        return Principal(org_id=1, user_id=2)

    app.dependency_overrides[get_principal] = ordinary_user
    try:
        client = TestClient(app)
        response = client.get("/api/v1/admin/resources/runtime-workspaces")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "admin work number permission is required"
    }
