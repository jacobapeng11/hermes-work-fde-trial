from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.api.v1.user.enterprise_knowledge import (
    get_enterprise_knowledge_settings,
    update_enterprise_knowledge_settings,
)
from app.main import app
from app.schemas.enterprise_knowledge import EnterpriseKnowledgeSettingsUpdate
from app.services.enterprise_knowledge_settings import EnterpriseKnowledgeSettingsRecord
from app.services.runtime_workspace import Principal


async def test_enterprise_knowledge_settings_get_and_put() -> None:
    class FakeService:
        async def get(self, principal, runtime_workspace_id):
            assert principal == Principal(org_id=1, user_id=1)
            assert runtime_workspace_id == 1
            return EnterpriseKnowledgeSettingsRecord(1, False, "full", None)

        async def update(self, principal, runtime_workspace_id, *, enabled):
            assert principal == Principal(org_id=1, user_id=1)
            assert runtime_workspace_id == 1
            assert enabled is True
            return EnterpriseKnowledgeSettingsRecord(
                1, True, "full", datetime(2026, 7, 16, tzinfo=timezone.utc)
            )

    service = FakeService()
    principal = Principal(org_id=1, user_id=1)
    current = await get_enterprise_knowledge_settings(1, principal, service)
    updated = await update_enterprise_knowledge_settings(
        1,
        EnterpriseKnowledgeSettingsUpdate(enabled=True),
        principal,
        service,
    )

    assert current.enabled is False
    assert updated.enabled is True
    assert updated.mode == "full"


def test_enterprise_knowledge_settings_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/v1/user/runtime-workspace/{runtime_workspace_id}/enterprise-knowledge" in paths


def test_enterprise_knowledge_settings_rejects_invalid_payload() -> None:
    with pytest.raises(ValidationError):
        EnterpriseKnowledgeSettingsUpdate(enabled="yes")
