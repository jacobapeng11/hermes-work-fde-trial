"""Backend-owned action policy for user-facing expert Profiles."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.experts import ExpertAllowedActions


@dataclass(frozen=True)
class ExpertActionContext:
    ownership_type: str
    lifecycle_status: str
    applied_revision: int
    operation_running: bool
    review_status: str | None = None


def decide_expert_actions(context: ExpertActionContext) -> ExpertAllowedActions:
    """Return actions from control-plane facts, never from UI status guesses."""

    active = context.lifecycle_status == "active"
    self_created = context.ownership_type == "self_created"
    market_installed = context.ownership_type == "market_installed"
    mutable = active and not context.operation_running
    runnable = active and context.applied_revision > 0

    return ExpertAllowedActions(
        view_detail=True,
        uninstall=market_installed and not context.operation_running,
        summon=runnable,
        edit=self_created and mutable,
        copy=self_created and mutable,
        publish=self_created and mutable and context.review_status != "pending",
        unlist=self_created and mutable and context.review_status == "approved",
        # A disabled self-created expert must remain removable so stale or
        # security-disabled records cannot trap the user in an unusable state.
        delete=(
            self_created
            and context.lifecycle_status != "deleted"
            and not context.operation_running
        ),
    )


__all__ = ["ExpertActionContext", "decide_expert_actions"]
