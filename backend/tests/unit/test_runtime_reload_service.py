from app.services.runtime_reload import (
    ProjectionScope,
    ReloadAction,
    ReloadDecisionInput,
    RuntimeReloadDecisionService,
    RuntimeStatus,
)


def decide(payload: ReloadDecisionInput):
    return RuntimeReloadDecisionService().decide(payload)


def test_absent_runtime_updates_template_and_allows_next_activation() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.ABSENT,
            scopes={ProjectionScope.MODEL},
        )
    )

    assert decision.action == ReloadAction.UPDATE_TEMPLATE
    assert decision.pending_reload is False
    assert decision.allow_new_run is True


def test_scaled_to_zero_runtime_updates_template() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.SCALED_TO_ZERO,
            scopes={ProjectionScope.SKILLS},
        )
    )

    assert decision.action == ReloadAction.UPDATE_TEMPLATE
    assert decision.pending_reload is False


def test_ready_idle_hot_reloads_non_restart_scope_when_capability_verified() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.READY_IDLE,
            scopes={ProjectionScope.PERSONA},
            hot_reload_supported=True,
        )
    )

    assert decision.action == ReloadAction.HOT_RELOAD
    assert decision.pending_reload is False
    assert decision.allow_new_run is True


def test_ready_idle_restarts_when_hot_reload_is_not_verified() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.READY_IDLE,
            scopes={ProjectionScope.PERSONA},
            hot_reload_supported=False,
        )
    )

    assert decision.action == ReloadAction.RESTART_POD
    assert decision.pending_reload is False


def test_ready_idle_restarts_for_model_scope_even_with_hot_reload() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.READY_IDLE,
            scopes={ProjectionScope.MODEL},
            hot_reload_supported=True,
        )
    )

    assert decision.action == ReloadAction.RESTART_POD
    assert decision.allow_new_run is True


def test_active_runtime_marks_pending_reload_for_non_strict_scope() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.ACTIVE,
            scopes={ProjectionScope.KNOWLEDGE},
            active_run_count=1,
        )
    )

    assert decision.action == ReloadAction.MARK_PENDING_RELOAD
    assert decision.pending_reload is True
    assert decision.allow_new_run is True


def test_active_runtime_rejects_new_run_for_security_scope() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.ACTIVE,
            scopes={ProjectionScope.SECURITY},
            active_run_count=1,
        )
    )

    assert decision.action == ReloadAction.REJECT_NEW_RUN_UNTIL_REFRESHED
    assert decision.pending_reload is True
    assert decision.allow_new_run is False


def test_active_runtime_rejects_new_run_for_model_policy_scope() -> None:
    decision = decide(
        ReloadDecisionInput(
            runtime_status=RuntimeStatus.ACTIVE,
            scopes={ProjectionScope.MODEL_POLICY},
            active_run_count=1,
        )
    )

    assert decision.action == ReloadAction.REJECT_NEW_RUN_UNTIL_REFRESHED
    assert decision.pending_reload is True
    assert decision.allow_new_run is False
