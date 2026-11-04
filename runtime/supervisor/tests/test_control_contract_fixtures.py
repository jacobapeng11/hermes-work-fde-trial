from __future__ import annotations

import json
from pathlib import Path

from platform_supervisor.contracts import RuntimeDashboardUpstreamErrorDetail

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "schemas"
    / "fixtures"
    / "runtime-control-v1"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_dashboard_upstream_error_fixture_is_bounded() -> None:
    payload = load_fixture("dashboard-upstream-error.json")

    assert payload == {
        "detail": {
            "code": "RUNTIME_DASHBOARD_UPSTREAM_ERROR",
            "message": "runtime Dashboard request failed",
            "upstream_status_code": 404,
            "upstream_path": "/api/files",
        }
    }
    parsed = RuntimeDashboardUpstreamErrorDetail.model_validate(payload["detail"])
    assert parsed.model_dump(mode="json", exclude_none=True) == payload["detail"]


def test_profile_mcp_list_fixture_uses_explicit_profile_scope() -> None:
    payload = load_fixture("list-profile-mcp-servers.json")

    assert payload["request"] == {
        "action": "list_profile_mcp_servers",
        "params": {"profile_name": "expert-7"},
    }
    assert payload["response"] == {
        "result": [{"name": "scoped-docs"}]
    }
