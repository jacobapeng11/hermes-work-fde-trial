from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from platform_supervisor.execution import PrepareIdentity
from platform_supervisor.model_relay import (
    ModelRouteRegistry,
    SessionModelRouter,
)
from platform_supervisor.settings import SupervisorSettings


def _settings(
    tmp_path: Path,
    *,
    runtime_mode: str = "application",
) -> SupervisorSettings:
    return SupervisorSettings(
        _env_file=None,
        RUNTIME_ID="application-runtime-25",
        RUNTIME_BINDING_ID="17",
        RUNTIME_MODE=runtime_mode,
        HERMES_STARTUP_MODE="steady_state",
        API_SERVER_KEY="runtime-only-secret-0123456789abcdef",
        NEWAPI_API_KEY="model-gateway-only-secret-0123456789abcdef",
        NEWAPI_BASE_URL="https://newapi.example/v1",
        HERMES_HOME=tmp_path / "hermes",
    )


def _identity() -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id="application-runtime-25",
        session_id="app-25-session-00000001",
        profile_id="application-25",
        model_id="19",
        config_revision="generation-31",
        admission_generation=11,
        max_output_tokens=32768,
    )


@pytest.mark.asyncio
async def test_application_native_vision_route_enables_hermes_native_input(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    config_path = configured.HERMES_HOME / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": {"default": "19", "provider": "custom:platform"},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())
    identity = replace(
        _identity(),
        runtime_capabilities={"vision": "native"},
    )

    await router.prepare(identity)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["model"]["supports_vision"] is True
    provider = document["providers"][router.provider_name(identity)]
    assert provider["models"]["19"]["supports_vision"] is True


@pytest.mark.asyncio
async def test_application_non_vision_route_disables_hermes_native_input(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    config_path = configured.HERMES_HOME / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "19",
                    "provider": "custom:platform",
                    "supports_vision": True,
                },
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())

    await router.prepare(_identity())

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["model"]["supports_vision"] is False


@pytest.mark.asyncio
async def test_personal_native_vision_route_uses_hermes_native_input(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path, runtime_mode="personal")
    configured.HERMES_HOME.mkdir(parents=True)
    config_path = configured.HERMES_HOME / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "agent": {},
                "model": {"default": "session", "provider": "custom:platform"},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())
    identity = replace(
        _identity(),
        upstream_model_id="deepseek-v4-flash",
        runtime_capabilities={"vision": "native"},
    )

    await router.prepare(identity)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["agent"]["image_input_mode"] == "native"
    assert "supports_vision" not in document["model"]
    assert (
        document["providers"][router.provider_name(identity)]["models"][
            "deepseek-v4-flash"
        ]["supports_vision"]
        is True
    )
    assert (
        document["providers"]["custom"]["models"]["deepseek-v4-flash"][
            "supports_vision"
        ]
        is True
    )


@pytest.mark.asyncio
async def test_personal_non_vision_route_does_not_roll_back_native_input(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path, runtime_mode="personal")
    configured.HERMES_HOME.mkdir(parents=True)
    config_path = configured.HERMES_HOME / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "agent": {"image_input_mode": "native"},
                "model": {"default": "session", "provider": "custom:platform"},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())

    await router.prepare(_identity())

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["agent"]["image_input_mode"] == "native"
    assert "supports_vision" not in document["model"]


@pytest.mark.asyncio
async def test_personal_native_vision_route_repairs_hermes_custom_capability(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path, runtime_mode="personal")
    configured.HERMES_HOME.mkdir(parents=True)
    config_path = configured.HERMES_HOME / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "agent": {"image_input_mode": "native"},
                "model": {"default": "session", "provider": "custom:platform"},
                "providers": {
                    "custom": {
                        "models": {
                            "deepseek-v4-flash": {"supports_vision": False}
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())
    identity = replace(
        _identity(),
        upstream_model_id="deepseek-v4-flash",
        runtime_capabilities={"vision": "native"},
    )

    await router.prepare(identity)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert (
        document["providers"]["custom"]["models"]["deepseek-v4-flash"][
            "supports_vision"
        ]
        is True
    )
