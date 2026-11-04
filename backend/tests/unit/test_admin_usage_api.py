from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.services.runtime_workspace import Principal


def test_admin_usage_summary_returns_runtime_model_and_error_summaries(
    admin_api_access,
) -> None:
    client = TestClient(app)

    response = client.get("/api/v1/admin/usage/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["run_count"] == 5
    assert payload["overview"]["active_run_count"] == 1
    assert payload["overview"]["model_call_count"] == 3
    assert payload["overview"]["total_tokens"] == 89
    assert payload["runtime_workspaces"][0]["runtime_workspace_id"] == 1
    assert payload["runtime_workspaces"][0]["total_tokens"] == 89
    assert payload["models"][0]["model_display_name"] == "GPT-4.1 Mini"
    assert payload["recent_errors"][0]["source"] == "model_relay"

    serialized = response.text.lower()
    assert "api_key" not in serialized
    assert "provider_key" not in serialized
    assert "runtime_token" not in serialized
    assert "kubeconfig" not in serialized
    assert "host_path" not in serialized


def test_admin_usage_summary_requires_admin_role() -> None:
    async def ordinary_user() -> Principal:
        return Principal(org_id=1, user_id=2)

    app.dependency_overrides[get_principal] = ordinary_user
    try:
        client = TestClient(app)
        response = client.get("/api/v1/admin/usage/summary")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "admin work number permission is required"
    }
