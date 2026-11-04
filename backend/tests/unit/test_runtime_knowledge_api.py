from app.main import app
from app.api.v1.runtime_knowledge import _bearer
from fastapi import HTTPException
import pytest


def test_runtime_knowledge_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/v1/runtime-knowledge/{runtime_workspace_id}/state" in paths
    assert "/api/v1/runtime-knowledge/{runtime_workspace_id}/sql/context" in paths
    assert "/api/v1/runtime-knowledge/{runtime_workspace_id}/sql/query" in paths
    assert "/api/v1/runtime-knowledge/{runtime_workspace_id}/documents/search" in paths


def test_runtime_knowledge_requires_bearer_token() -> None:
    with pytest.raises(HTTPException) as exc:
        _bearer(None)
    assert exc.value.status_code == 401
    assert _bearer("Bearer runtime-token") == "runtime-token"
