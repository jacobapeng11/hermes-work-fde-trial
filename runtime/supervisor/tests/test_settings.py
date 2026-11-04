from __future__ import annotations

from pydantic import ValidationError
import pytest

from platform_supervisor.settings import SupervisorSettings


def application_settings(**overrides: object) -> SupervisorSettings:
    values = {
        "RUNTIME_ID": "runtime-application-3",
        "RUNTIME_BINDING_ID": "binding-3",
        "RUNTIME_MODE": "application",
        "HERMES_STARTUP_MODE": "steady_state",
        "API_SERVER_KEY": "runtime-only-secret-0123456789abcdef",
        "NEWAPI_API_KEY": "model-gateway-only-secret-0123456789",
        "NEWAPI_BASE_URL": "https://newapi.example/v1",
    }
    values.update(overrides)
    return SupervisorSettings(_env_file=None, **values)


def test_application_capacity_defaults_match_confirmed_contract() -> None:
    settings = application_settings()

    assert settings.max_inflight_runs == 500
    assert settings.max_resident_sessions == 564
    assert settings.max_external_websockets == 2000
    assert settings.model_relay_max_connections == 600
    assert settings.model_relay_max_keepalive_connections == 128
    assert settings.APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS == 600
    assert settings.APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS == 900


@pytest.mark.parametrize("value", ["33", "34"])
def test_expected_config_schema_accepts_canonical_environment_strings(
    value: str,
) -> None:
    settings = application_settings(HERMES_EXPECTED_CONFIG_SCHEMA=value)

    assert settings.HERMES_EXPECTED_CONFIG_SCHEMA == int(value)


def test_personal_runtime_defaults_to_ten_runs_and_four_idle_residents() -> None:
    settings = application_settings(RUNTIME_MODE="personal")

    assert settings.max_inflight_runs == 10
    assert settings.max_resident_sessions == 14
    assert settings.model_relay_max_connections == 12


def test_personal_runtime_capacity_is_validated() -> None:
    assert application_settings(
        RUNTIME_MODE="personal",
            HERMES_STARTUP_MODE="steady_state",
        PERSONAL_MAX_INFLIGHT_RUNS=7,
    ).max_inflight_runs == 7

    with pytest.raises(ValidationError):
        application_settings(
            RUNTIME_MODE="personal",
            HERMES_STARTUP_MODE="steady_state",
            PERSONAL_MAX_INFLIGHT_RUNS=0,
        )


def test_post_run_warm_grace_cannot_exceed_hot_session_ttl() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        application_settings(
            APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS=901,
            APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        )
