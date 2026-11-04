from fastapi.testclient import TestClient

from app.main import app


def test_runtime_capabilities_api_returns_enabled_capability_summaries_without_secrets() -> None:
    client = TestClient(app)

    response = client.get("/api/v1/user/runtime-workspace/1/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime_workspace_id"] == 1
    assert payload["experts"][0] == {
        "asset_type": "expert_package",
        "asset_id": 1,
        "display_name": "Sales Expert",
        "status": "enabled",
        "source_type": "personal",
    }
    forbidden = response.text.lower()
    assert "api_key" not in forbidden
    assert "api_key_encrypted" not in forbidden
    assert "mcp_secret" not in forbidden
    assert "runtime_token" not in forbidden
    assert "host_path" not in forbidden


def test_runtime_capabilities_api_keeps_runtime_workspace_owner_boundary() -> None:
    client = TestClient(app)

    response = client.get("/api/v1/user/runtime-workspace/999/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "runtime_workspace_id": 999,
        "experts": [],
        "skills": [],
        "mcps": [],
        "knowledge_bases": [],
    }
