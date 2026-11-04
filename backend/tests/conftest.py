from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("HERMES_AUTH_MODE", "dev")
os.environ.setdefault("RUNTIME_CHAT_MODE", "contract")
os.environ.setdefault("DEV_PRINCIPAL_USER_ID", "1")
os.environ.setdefault("HERMES_RUNTIME_PROVIDER", "knative")


@pytest.fixture
def admin_api_access():
    """Grant explicit route-level admin access to API contract tests."""

    from app.api.v1.admin.deps import get_admin_principal
    from app.main import app
    from app.services.admin_auth import AdminPrincipal

    async def admin_principal() -> AdminPrincipal:
        return AdminPrincipal(org_id=1, user_id=1, roles=("admin",))

    previous = app.dependency_overrides.get(get_admin_principal)
    app.dependency_overrides[get_admin_principal] = admin_principal
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_admin_principal, None)
        else:
            app.dependency_overrides[get_admin_principal] = previous
