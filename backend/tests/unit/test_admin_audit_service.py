from __future__ import annotations

import pytest

from app.services.admin_audit import AdminAuditLogService, InMemoryAdminAuditLogStore
from app.services.admin_auth import AdminPrincipal


def admin() -> AdminPrincipal:
    return AdminPrincipal(org_id=1, user_id=1, roles=("admin",))


@pytest.mark.asyncio
async def test_admin_audit_records_and_filters_logs() -> None:
    store = InMemoryAdminAuditLogStore()
    service = AdminAuditLogService(store)

    await service.record(
        principal=admin(),
        action="admin.model.status_updated",
        target_type="model_entry",
        target_id=2,
        detail={"status": "disabled"},
    )
    await service.record(
        principal=admin(),
        action="admin.runtime.force_stop",
        target_type="runtime_workspace",
        target_id=1,
        runtime_workspace_id=1,
        detail={"status": "force_stopped"},
    )

    result = await service.list_logs(principal=admin(), target_type="runtime_workspace")

    assert len(result) == 1
    assert result[0].action == "admin.runtime.force_stop"
    assert result[0].runtime_workspace_id == 1


@pytest.mark.asyncio
async def test_admin_audit_sanitizes_secret_detail_keys() -> None:
    service = AdminAuditLogService(InMemoryAdminAuditLogStore())

    record = await service.record(
        principal=admin(),
        action="admin.public_asset.published",
        target_type="public_asset",
        target_id=1,
        detail={
            "status": "approved",
            "api_key": "secret",
            "nested": {"provider_key": "secret", "safe": "kept"},
        },
    )

    assert record.detail == {"status": "approved", "nested": {"safe": "kept"}}
