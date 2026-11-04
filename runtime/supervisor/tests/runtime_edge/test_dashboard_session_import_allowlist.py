from __future__ import annotations

import pytest
from fastapi import HTTPException

from platform_supervisor.edge import _validate_dashboard_control_request


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/sessions/import"),
        ("POST", "/api/profiles/expert-7/description"),
        ("GET", "/api/config/raw"),
    ],
)
def test_dashboard_control_allows_expected_management_paths(method, path) -> None:
    _validate_dashboard_control_request(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/sessions/import"),
        ("POST", "/api/sessions"),
        ("POST", "/api/sessions/../sessions/import"),
        ("POST", "/api/sessions/import/extra"),
    ],
)
def test_dashboard_control_keeps_other_session_paths_blocked(method, path) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_dashboard_control_request(method, path)
    assert exc_info.value.status_code == 403
