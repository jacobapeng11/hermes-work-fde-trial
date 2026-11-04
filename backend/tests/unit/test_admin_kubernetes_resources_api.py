from __future__ import annotations

import httpx
import pytest

from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.services.runtime_workspace import Principal


pytestmark = pytest.mark.usefixtures("admin_api_access")


async def get(path: str) -> httpx.Response:
    async def admin_user() -> Principal:
        return Principal(org_id=1, user_id=1)

    app.dependency_overrides[get_principal] = admin_user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)
    finally:
        app.dependency_overrides.pop(get_principal, None)


@pytest.mark.asyncio
async def test_cluster_summary_exposes_health_without_credentials() -> None:
    response = await get("/api/v1/admin/resources/cluster-summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["node_count"] == 1
    assert payload["ready_node_count"] == 1
    assert payload["knative_control_plane_ready"] is True
    assert payload["capacity"]["pending_pod_count"] == 0
    assert "request_ratio" in payload["capacity"]
    serialized = response.text.lower()
    assert "kubeconfig" not in serialized
    assert "token" not in serialized
    assert "host_path" not in serialized


@pytest.mark.asyncio
async def test_runtime_snapshot_is_org_scoped_and_safe() -> None:
    response = await get(
        "/api/v1/admin/resources/runtime-workspaces/1/kubernetes"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime_workspace_id"] == 1
    assert payload["resource_key"] == "personal:1"
    assert payload["runtime_kind"] == "personal"
    assert payload["runtime_service"]["kservice_name"] == "runtime-employee-7"
    assert "gateway_service" not in payload
    assert "api_service" not in payload
    assert "stream_service" not in payload
    assert payload["aggregate_status"] == "scaled_to_zero"
    assert "secret" not in response.text.lower()


@pytest.mark.asyncio
async def test_runtime_snapshot_list_returns_one_batch_for_current_org() -> None:
    response = await get(
        "/api/v1/admin/resources/runtime-workspaces/kubernetes-snapshots"
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["runtime_workspace_id"] for item in payload] == [1]
    assert payload[0]["runtime_service"]["kservice_name"] == "runtime-employee-7"
    assert payload[0]["resource_key"] == "personal:1"


@pytest.mark.asyncio
async def test_missing_runtime_snapshot_returns_404() -> None:
    response = await get(
        "/api/v1/admin/resources/runtime-workspaces/404/kubernetes"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "runtime workspace not found"}


@pytest.mark.asyncio
async def test_safe_yaml_summary_is_allowlist_only() -> None:
    response = await get(
        "/api/v1/admin/resources/runtime-workspaces/1/kubernetes/yaml-summary"
    )

    assert response.status_code == 200
    serialized = response.text.lower()
    assert "runtime-employee-7" in serialized
    assert "env" not in serialized
    assert "secretkeyref" not in serialized
    assert "hostpath" not in serialized
    assert "endpointslice" not in serialized


@pytest.mark.asyncio
async def test_logs_reject_more_than_2000_lines_before_kubernetes_access() -> None:
    response = await get(
        "/api/v1/admin/resources/runtime-workspaces/1/pods/runtime-pod/logs"
        "?container=user-container&tail_lines=2001"
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_runtime_kind_rejects_unknown_resource_type() -> None:
    response = await get(
        "/api/v1/admin/resources/runtime-workspaces/1/kubernetes"
        "?runtime_kind=unknown"
    )

    assert response.status_code == 422
