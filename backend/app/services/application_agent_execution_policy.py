from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApplicationAgentConfigRevision
from app.services.runtime_models import (
    RuntimeModelRecord,
    SqlAlchemyRuntimeModelStore,
    resolve_default_model_entry_id,
)


class ApplicationAgentExecutionPolicyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApplicationAgentModelPolicy:
    model_entry_id: int
    source: str


@dataclass(frozen=True, slots=True)
class ApplicationAgentAppliedConfig:
    generation: int
    config: dict[str, Any]


def select_application_agent_model(
    *,
    configured_model_entry_id: object,
    visible_models: Sequence[RuntimeModelRecord],
) -> ApplicationAgentModelPolicy:
    visible_by_id = {
        int(model.model_entry_id): model
        for model in visible_models
    }
    if configured_model_entry_id is not None:
        try:
            configured = int(configured_model_entry_id)
        except (TypeError, ValueError) as exc:
            raise ApplicationAgentExecutionPolicyError(
                "application_agent_model_invalid"
            ) from exc
        if configured <= 0 or configured not in visible_by_id:
            raise ApplicationAgentExecutionPolicyError(
                "application_agent_model_unavailable"
            )
        return ApplicationAgentModelPolicy(
            model_entry_id=configured,
            source="application",
        )

    default_model_entry_id = resolve_default_model_entry_id(visible_models)
    if default_model_entry_id is None:
        raise ApplicationAgentExecutionPolicyError(
            "application_agent_model_unavailable"
        )
    return ApplicationAgentModelPolicy(
        model_entry_id=default_model_entry_id,
        source="platform_default",
    )


class ApplicationAgentExecutionPolicyResolver:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve_model(
        self,
        *,
        org_id: int,
        owner_user_id: int,
        config: dict[str, Any] | None,
    ) -> ApplicationAgentModelPolicy:
        visible_models = await SqlAlchemyRuntimeModelStore(
            self.db
        ).list_visible_models(
            org_id=org_id,
            user_id=owner_user_id,
        )
        return select_application_agent_model(
            configured_model_entry_id=(config or {}).get(
                "model_entry_id"
            ),
            visible_models=visible_models,
        )


async def resolve_application_execution_config(
    db: AsyncSession,
    *,
    agent: Any,
    binding: Any,
) -> ApplicationAgentAppliedConfig:
    desired_generation = int(agent.config_generation or 0)
    applied_generation = int(binding.config_generation or 0)
    if (
        desired_generation <= 0
        or applied_generation <= 0
        or applied_generation > desired_generation
    ):
        raise ApplicationAgentExecutionPolicyError(
            "application_agent_applied_config_invalid"
        )
    if applied_generation == desired_generation:
        return ApplicationAgentAppliedConfig(
            generation=applied_generation,
            config=dict(agent.config or {}),
        )
    revision = await db.scalar(
        select(ApplicationAgentConfigRevision).where(
            ApplicationAgentConfigRevision.application_agent_id
            == int(agent.id),
            ApplicationAgentConfigRevision.generation
            == applied_generation,
            ApplicationAgentConfigRevision.apply_status == "applied",
        )
    )
    if revision is None:
        raise ApplicationAgentExecutionPolicyError(
            "application_agent_applied_config_missing"
        )
    return ApplicationAgentAppliedConfig(
        generation=applied_generation,
        config=dict(revision.config_snapshot or {}),
    )


__all__ = [
    "ApplicationAgentExecutionPolicyError",
    "ApplicationAgentExecutionPolicyResolver",
    "ApplicationAgentAppliedConfig",
    "ApplicationAgentModelPolicy",
    "resolve_application_execution_config",
    "select_application_agent_model",
]
