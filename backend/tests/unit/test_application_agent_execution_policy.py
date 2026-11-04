from __future__ import annotations

import pytest

from app.services.application_agent_execution_policy import (
    ApplicationAgentExecutionPolicyError,
    resolve_application_execution_config,
    select_application_agent_model,
)
from app.services.runtime_models import RuntimeModelRecord


def _model(model_entry_id: int, *, is_default: bool = False) -> RuntimeModelRecord:
    return RuntimeModelRecord(
        model_entry_id=model_entry_id,
        model_key=f"model-{model_entry_id}",
        display_name=f"Model {model_entry_id}",
        provider_id=10 + model_entry_id,
        provider_display_name="Provider",
        context_length=128_000,
        tags=[],
        is_default=is_default,
    )


def test_null_model_dynamically_selects_visible_platform_default() -> None:
    policy = select_application_agent_model(
        configured_model_entry_id=None,
        visible_models=[_model(1), _model(7, is_default=True)],
    )

    assert policy.model_entry_id == 7
    assert policy.source == "platform_default"


def test_explicit_model_remains_authoritative() -> None:
    policy = select_application_agent_model(
        configured_model_entry_id=1,
        visible_models=[_model(1), _model(7, is_default=True)],
    )

    assert policy.model_entry_id == 1
    assert policy.source == "application"


@pytest.mark.parametrize("configured", ["invalid", 0, 99])
def test_invalid_or_unavailable_explicit_model_fails_closed(
    configured: object,
) -> None:
    with pytest.raises(
        ApplicationAgentExecutionPolicyError,
        match="application_agent_model_",
    ):
        select_application_agent_model(
            configured_model_entry_id=configured,
            visible_models=[_model(7, is_default=True)],
        )


def test_null_model_falls_back_to_first_visible_when_default_is_not_visible() -> None:
    policy = select_application_agent_model(
        configured_model_entry_id=None,
        visible_models=[_model(3), _model(1)],
    )

    assert policy.model_entry_id == 1
    assert policy.source == "platform_default"


@pytest.mark.asyncio
async def test_pending_application_config_uses_last_applied_snapshot() -> None:
    class Db:
        async def scalar(self, _statement: object) -> object:
            return type(
                "Revision",
                (),
                {
                    "generation": 5,
                    "config_snapshot": {"model_entry_id": 1},
                    "apply_status": "applied",
                },
            )()

    agent = type(
        "Agent",
        (),
        {
            "id": 31,
            "config_generation": 6,
            "config": {"model_entry_id": 7},
        },
    )()
    binding = type("Binding", (), {"config_generation": 5})()
    db = Db()

    applied = await resolve_application_execution_config(
        db,  # type: ignore[arg-type]
        agent=agent,
        binding=binding,
    )

    assert applied.generation == 5
    assert applied.config == {"model_entry_id": 1}


@pytest.mark.asyncio
async def test_current_application_config_uses_agent_snapshot_without_query() -> None:
    class Db:
        async def scalar(self, _statement: object) -> object:
            raise AssertionError("current generation must not query a revision")

    agent = type(
        "Agent",
        (),
        {
            "id": 31,
            "config_generation": 6,
            "config": {"model_entry_id": 7},
        },
    )()
    binding = type("Binding", (), {"config_generation": 6})()

    applied = await resolve_application_execution_config(
        Db(),  # type: ignore[arg-type]
        agent=agent,
        binding=binding,
    )

    assert applied.generation == 6
    assert applied.config == {"model_entry_id": 7}
