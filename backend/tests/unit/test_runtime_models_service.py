import pytest

from app.services.runtime_models import (
    DefaultModelUpdateIntent,
    RuntimeModelNotVisibleError,
    RuntimeModelRecord,
    RuntimeSessionModelState,
    RuntimeSessionModelSwitchNotAllowedError,
    RuntimeTurnModelNotVisibleError,
    RuntimeTurnModelUnavailableError,
    SessionModelSelectionIntent,
    RuntimeModelService,
    resolve_default_model_entry_id,
    resolve_runtime_turn_model_entry_id,
)
from app.services.runtime_workspace import Principal


class FakeRuntimeModelStore:
    def __init__(self) -> None:
        self.visible_models = [
            RuntimeModelRecord(
                model_entry_id=10,
                model_key="model-a",
                display_name="Model A",
                provider_id=1,
                provider_display_name="Provider A",
                is_default=True,
            )
        ]
        self.updated: list[dict] = []
        self.session_state: RuntimeSessionModelState | None = None
        self.session_updates: list[dict] = []

    async def list_visible_models(self, *, org_id: int, user_id: int):
        return self.visible_models

    async def set_default_model(self, *, org_id: int, user_id: int, runtime_workspace_id: int, model_entry_id: int):
        self.updated.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_workspace_id": runtime_workspace_id,
                "model_entry_id": model_entry_id,
            }
        )
        return DefaultModelUpdateIntent(
            runtime_workspace_id=runtime_workspace_id,
            model_entry_id=model_entry_id,
            projection_scope="model",
            pending_reload=True,
        )

    async def get_session_model_state(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ):
        return self.session_state

    async def set_next_session_model(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        model_entry_id: int,
    ):
        self.session_updates.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_workspace_id": runtime_workspace_id,
                "runtime_session_id": runtime_session_id,
                "model_entry_id": model_entry_id,
            }
        )
        return SessionModelSelectionIntent(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            model_entry_id=model_entry_id,
            applies_to="next_run",
            active_run_count=0,
        )


async def test_list_models_returns_visible_model_records() -> None:
    store = FakeRuntimeModelStore()

    result = await RuntimeModelService(store).list_models(Principal(org_id=7, user_id=9))

    assert [model.model_entry_id for model in result] == [10]
    assert result[0].provider_display_name == "Provider A"


def test_resolve_default_model_prefers_visible_admin_default() -> None:
    models = [
        RuntimeModelRecord(
            model_entry_id=7,
            model_key="model-a",
            display_name="Model A",
            provider_id=1,
            provider_display_name="Provider",
        ),
        RuntimeModelRecord(
            model_entry_id=21,
            model_key="model-b",
            display_name="Model B",
            provider_id=1,
            provider_display_name="Provider",
            is_default=True,
        ),
    ]

    assert resolve_default_model_entry_id(models) == 21


def test_resolve_default_model_falls_back_to_first_visible() -> None:
    assert resolve_default_model_entry_id(
        [
            RuntimeModelRecord(
                model_entry_id=7,
                model_key="model-a",
                display_name="Model A",
                provider_id=1,
                provider_display_name="Provider",
            )
        ]
    ) == 7
    assert resolve_default_model_entry_id([]) is None


def test_resolve_runtime_turn_model_uses_shared_priority() -> None:
    models = [
        RuntimeModelRecord(
            model_entry_id=7,
            model_key="model-a",
            display_name="Model A",
            provider_id=1,
            provider_display_name="Provider",
        ),
        RuntimeModelRecord(
            model_entry_id=21,
            model_key="model-b",
            display_name="Model B",
            provider_id=1,
            provider_display_name="Provider",
            is_default=True,
        ),
    ]

    assert resolve_runtime_turn_model_entry_id(
        requested_model_entry_id=7,
        session_model_entry_id=21,
        workspace_default_model_entry_id=None,
        visible_models=models,
    ) == 7
    assert resolve_runtime_turn_model_entry_id(
        requested_model_entry_id=None,
        session_model_entry_id=7,
        workspace_default_model_entry_id=21,
        visible_models=models,
    ) == 7
    assert resolve_runtime_turn_model_entry_id(
        requested_model_entry_id=None,
        session_model_entry_id=None,
        workspace_default_model_entry_id=7,
        visible_models=models,
    ) == 7
    assert resolve_runtime_turn_model_entry_id(
        requested_model_entry_id=None,
        session_model_entry_id=None,
        workspace_default_model_entry_id=None,
        visible_models=models,
    ) == 21


def test_resolve_runtime_turn_model_falls_back_stably() -> None:
    models = [
        RuntimeModelRecord(
            model_entry_id=11,
            model_key="model-b",
            display_name="Model B",
            provider_id=1,
            provider_display_name="Provider",
        ),
        RuntimeModelRecord(
            model_entry_id=7,
            model_key="model-a",
            display_name="Model A",
            provider_id=1,
            provider_display_name="Provider",
        ),
    ]

    assert resolve_runtime_turn_model_entry_id(
        requested_model_entry_id=None,
        session_model_entry_id=None,
        workspace_default_model_entry_id=None,
        visible_models=models,
    ) == 7


def test_resolve_runtime_turn_model_fails_closed() -> None:
    model = RuntimeModelRecord(
        model_entry_id=7,
        model_key="model-a",
        display_name="Model A",
        provider_id=1,
        provider_display_name="Provider",
    )

    with pytest.raises(RuntimeTurnModelNotVisibleError):
        resolve_runtime_turn_model_entry_id(
            requested_model_entry_id=None,
            session_model_entry_id=99,
            workspace_default_model_entry_id=None,
            visible_models=[model],
        )
    with pytest.raises(RuntimeTurnModelNotVisibleError):
        resolve_runtime_turn_model_entry_id(
            requested_model_entry_id=None,
            session_model_entry_id=None,
            workspace_default_model_entry_id=99,
            visible_models=[model],
        )
    with pytest.raises(RuntimeTurnModelUnavailableError):
        resolve_runtime_turn_model_entry_id(
            requested_model_entry_id=None,
            session_model_entry_id=None,
            workspace_default_model_entry_id=None,
            visible_models=[],
        )


async def test_set_default_model_records_projection_intent_for_visible_model() -> None:
    store = FakeRuntimeModelStore()

    result = await RuntimeModelService(store).set_default_model(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=123,
        model_entry_id=10,
    )

    assert result.runtime_workspace_id == 123
    assert result.model_entry_id == 10
    assert result.projection_scope == "model"
    assert result.pending_reload is True
    assert store.updated == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_workspace_id": 123,
            "model_entry_id": 10,
        }
    ]


async def test_set_default_model_rejects_non_visible_model() -> None:
    store = FakeRuntimeModelStore()

    try:
        await RuntimeModelService(store).set_default_model(
            principal=Principal(org_id=7, user_id=9),
            runtime_workspace_id=123,
            model_entry_id=99,
        )
    except RuntimeModelNotVisibleError:
        pass
    else:
        raise AssertionError("expected RuntimeModelNotVisibleError")


async def test_select_session_model_for_new_session_applies_to_next_run() -> None:
    store = FakeRuntimeModelStore()

    result = await RuntimeModelService(store).select_session_model(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=123,
        runtime_session_id="session-1",
        model_entry_id=10,
    )

    assert result.runtime_session_id == "session-1"
    assert result.model_entry_id == 10
    assert result.applies_to == "next_run"
    assert store.session_updates == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_workspace_id": 123,
            "runtime_session_id": "session-1",
            "model_entry_id": 10,
        }
    ]


async def test_select_session_model_rejects_active_run_switch() -> None:
    store = FakeRuntimeModelStore()
    store.session_state = RuntimeSessionModelState(
        runtime_workspace_id=123,
        runtime_session_id="session-1",
        current_model_entry_id=10,
        next_model_entry_id=None,
        active_run_count=1,
    )

    try:
        await RuntimeModelService(store).select_session_model(
            principal=Principal(org_id=7, user_id=9),
            runtime_workspace_id=123,
            runtime_session_id="session-1",
            model_entry_id=10,
        )
    except RuntimeSessionModelSwitchNotAllowedError:
        pass
    else:
        raise AssertionError("expected RuntimeSessionModelSwitchNotAllowedError")

    assert store.session_updates == []


async def test_select_session_model_rejects_non_visible_model() -> None:
    store = FakeRuntimeModelStore()

    try:
        await RuntimeModelService(store).select_session_model(
            principal=Principal(org_id=7, user_id=9),
            runtime_workspace_id=123,
            runtime_session_id="session-1",
            model_entry_id=99,
        )
    except RuntimeModelNotVisibleError:
        pass
    else:
        raise AssertionError("expected RuntimeModelNotVisibleError")
