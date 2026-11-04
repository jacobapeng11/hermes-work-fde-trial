from __future__ import annotations

import pytest

from app.services.admin_auth import AdminPrincipal
from app.services.admin_models import (
    AdminModelGovernanceError,
    AdminModelGovernanceService,
    InMemoryAdminModelGovernanceStore,
)


def admin() -> AdminPrincipal:
    return AdminPrincipal(org_id=1, user_id=1, roles=("admin",))


async def test_admin_lists_models_without_provider_secret_fields() -> None:
    result = await AdminModelGovernanceService(InMemoryAdminModelGovernanceStore()).list_models(admin())

    assert [model.model_entry_id for model in result] == [1, 2]
    assert result[0].provider_key == "platform-openai"
    assert not hasattr(result[0], "api_key")
    assert not hasattr(result[0], "api_key_encrypted")
    assert not hasattr(result[0], "api_endpoint")


async def test_admin_disables_model_and_records_upgrade_log_intent() -> None:
    store = InMemoryAdminModelGovernanceStore()

    result = await AdminModelGovernanceService(store).set_model_status(
        principal=admin(),
        model_entry_id=1,
        status="disabled",
    )

    assert result.model_entry_id == 1
    assert result.status == "disabled"
    assert result.upgrade_log_id == 1
    assert (await store.list_models(org_id=1))[0].status == "disabled"


async def test_admin_sets_active_default_model() -> None:
    store = InMemoryAdminModelGovernanceStore()

    result = await AdminModelGovernanceService(store).set_default_model(
        principal=admin(),
        model_entry_id=2,
    )

    models = await store.list_models(org_id=1)
    assert result.model_entry_id == 2
    assert result.is_default is True
    assert [model.model_entry_id for model in models if model.is_default] == [2]


async def test_admin_rejects_invalid_model_status() -> None:
    with pytest.raises(AdminModelGovernanceError, match="model status must be active or disabled"):
        await AdminModelGovernanceService(InMemoryAdminModelGovernanceStore()).set_model_status(
            principal=admin(),
            model_entry_id=1,
            status="deleted",
        )


async def test_admin_rejects_disabled_default_model() -> None:
    store = InMemoryAdminModelGovernanceStore()
    await AdminModelGovernanceService(store).set_model_status(
        principal=admin(),
        model_entry_id=2,
        status="disabled",
    )

    with pytest.raises(AdminModelGovernanceError, match="default model must be active"):
        await AdminModelGovernanceService(store).set_default_model(
            principal=admin(),
            model_entry_id=2,
        )
