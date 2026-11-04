from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.protocol import (
    HermesProtocolError,
    RuntimeDashboardUpstreamError,
)


FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "schemas"
    / "fixtures"
    / "runtime-control-v1"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_control_client_recovers_bounded_dashboard_upstream_error() -> None:
    response = httpx.Response(
        502,
        json=load_fixture("dashboard-upstream-error.json"),
    )

    with pytest.raises(HermesProtocolError) as captured:
        HermesDashboardProtocolAdapter._raise_for_status(
            response,
            path="/internal/v1/control",
        )

    assert captured.value.status_code == 404
    assert captured.value.path == "/api/files"
    assert str(captured.value) == "runtime Dashboard request failed"
    assert RuntimeDashboardUpstreamError.parse(
        load_fixture("dashboard-upstream-error.json")["detail"]
    ) == RuntimeDashboardUpstreamError(status_code=404, path="/api/files")


def test_profile_mcp_list_fixture_uses_explicit_profile_scope() -> None:
    payload = load_fixture("list-profile-mcp-servers.json")

    assert payload["request"]["params"]["profile_name"] == "expert-7"
    assert payload["response"]["result"] == [{"name": "scoped-docs"}]


@pytest.mark.parametrize(
    "detail",
    [
        "/api/files failed with status 404",
        {"code": "UNKNOWN", "upstream_status_code": 404, "upstream_path": "/api/files"},
        {
            "code": "RUNTIME_DASHBOARD_UPSTREAM_ERROR",
            "upstream_status_code": 200,
            "upstream_path": "/api/files",
        },
        {
            "code": "RUNTIME_DASHBOARD_UPSTREAM_ERROR",
            "upstream_status_code": 404,
            "upstream_path": "https://secret.invalid/api/files",
        },
    ],
)
def test_control_client_fails_closed_for_untrusted_error_detail(detail: object) -> None:
    response = httpx.Response(502, json={"detail": detail})

    with pytest.raises(HermesProtocolError) as captured:
        HermesDashboardProtocolAdapter._raise_for_status(
            response,
            path="/internal/v1/control",
        )

    assert captured.value.status_code == 502
    assert captured.value.path == "/internal/v1/control"
