from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from platform_supervisor.execution import PrepareIdentity
from platform_supervisor.model_relay import (
    ModelRelayRequestRejected,
    ModelRouteRegistry,
    SessionModelRouter,
)
from platform_supervisor.provider_routes import (
    ProviderRouteBindingIdentity,
    ProviderRouteMaterial,
)
from platform_supervisor.settings import SupervisorSettings


def settings(tmp_path: Path) -> SupervisorSettings:
    return SupervisorSettings(
        _env_file=None,
        RUNTIME_ID="runtime-application-3",
        RUNTIME_BINDING_ID="binding-3",
        RUNTIME_MODE="application",
            HERMES_STARTUP_MODE="steady_state",
        API_SERVER_KEY="runtime-only-secret-0123456789abcdef",
        NEWAPI_API_KEY="model-gateway-only-secret-0123456789",
        NEWAPI_BASE_URL="https://newapi.example/v1",
        HERMES_HOME=tmp_path / "hermes",
    )


def identity() -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id="runtime-application-3",
        session_id="session-01234567",
        profile_id="default",
        model_id="17",
        config_revision="revision-7",
        admission_generation=11,
        max_output_tokens=8192,
    )


class _ProviderRouteCodec:
    def open(self, **_kwargs) -> ProviderRouteMaterial:
        return ProviderRouteMaterial(
            org_id=1,
            model_entry_id=21,
            provider_id=17,
            upstream_model_id="qwen3.7-plus",
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="dashscope-secret-value",
            model_catalog_version=35,
            provider_catalog_version=35,
            provider_route_digest=("sha256:" + "1" * 64),
        )


def _provider_binding() -> ProviderRouteBindingIdentity:
    return ProviderRouteBindingIdentity(
        runtime_id="runtime-application-3",
        binding_type="application_runtime",
        binding_id=3,
        pvc_storage_identity_hash="sha256:" + "a" * 64,
    )


@pytest.mark.asyncio
async def test_session_provider_is_bounded_and_run_route_is_exact(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    (configured.HERMES_HOME / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "17"}, "providers": {}}),
        encoding="utf-8",
    )
    registry = ModelRouteRegistry()
    router = SessionModelRouter(configured, registry)
    prepared = identity()

    await router.prepare(prepared)

    document = yaml.safe_load(
        (configured.HERMES_HOME / "config.yaml").read_text(encoding="utf-8")
    )
    provider_name = router.provider_name(prepared)
    provider = document["providers"][provider_name]
    assert provider["base_url"] == router.relay_base_url(prepared)
    assert provider["key_env"] == "OPENAI_API_KEY"
    assert provider["models"] == {
        "17": {"context_length": 128_000, "supports_vision": False}
    }
    assert router.hermes_provider(prepared) == f"custom:{provider_name}"
    assert await registry.model_listing(router.route_token(prepared)) == "17"

    await router.activate(prepared, "run-01234567")
    route = await registry.get(router.route_token(prepared))
    assert route.run_id == "run-01234567"
    assert route.session_id == prepared.session_id
    assert route.model_id == "17"
    assert route.max_output_tokens == 8192

    await router.revoke_run("run-01234567")
    await router.close_session(prepared)
    document = yaml.safe_load(
        (configured.HERMES_HOME / "config.yaml").read_text(encoding="utf-8")
    )
    assert provider_name not in document.get("providers", {})
    assert "providers" not in document


@pytest.mark.asyncio
async def test_session_provider_enables_vision_only_for_native_capability(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    (configured.HERMES_HOME / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "17"}, "providers": {}}),
        encoding="utf-8",
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())
    prepared = replace(
        identity(),
        runtime_capabilities={"vision": "native"},
    )

    await router.prepare(prepared)

    document = yaml.safe_load(
        (configured.HERMES_HOME / "config.yaml").read_text(encoding="utf-8")
    )
    provider = document["providers"][router.provider_name(prepared)]
    assert provider["models"]["17"]["supports_vision"] is True


@pytest.mark.asyncio
async def test_session_provider_uses_signed_model_context_for_budget(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    (configured.HERMES_HOME / "config.yaml").write_text(
        yaml.safe_dump({"providers": {}}),
        encoding="utf-8",
    )
    registry = ModelRouteRegistry()
    router = SessionModelRouter(configured, registry)
    prepared = replace(
        identity(),
        max_output_tokens=32768,
        context_length=1_000_000,
    )

    await router.prepare(prepared)
    await router.activate(prepared, "run-context-0001")

    provider = yaml.safe_load(
        (configured.HERMES_HOME / "config.yaml").read_text(encoding="utf-8")
    )["providers"][router.provider_name(prepared)]
    route = await registry.get(router.route_token(prepared))
    assert provider["models"]["17"]["context_length"] == 1_000_000
    assert provider["max_output_tokens"] == 32768
    assert route.context_length == 1_000_000
    assert route.max_output_tokens == 32768

    await router.revoke_run("run-context-0001")
    await router.close_session(prepared)


@pytest.mark.asyncio
async def test_small_context_provider_reduces_output_reservation(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    path = configured.HERMES_HOME / "config.yaml"
    path.write_text(yaml.safe_dump({"providers": {}}), encoding="utf-8")
    router = SessionModelRouter(configured, ModelRouteRegistry())
    prepared = replace(identity(), max_output_tokens=32768)

    await router.prepare(prepared)

    provider = yaml.safe_load(path.read_text(encoding="utf-8"))["providers"][
        router.provider_name(prepared)
    ]
    assert provider["max_output_tokens"] == 16000
    assert provider["models"]["17"]["context_length"] == 128_000
    await router.close_session(prepared)


@pytest.mark.asyncio
async def test_named_profile_provider_uses_bound_dashboard_config_io(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    registry = ModelRouteRegistry()
    router = SessionModelRouter(configured, registry)
    prepared = replace(identity(), profile_id="expert-125")
    document: dict[str, object] = {
        "model": {"default": "17"},
        "providers": {},
    }
    writes: list[tuple[str, dict[str, object]]] = []

    async def read(profile_name: str) -> dict[str, object]:
        assert profile_name == "expert-125"
        return yaml.safe_load(yaml.safe_dump(document))

    async def write(profile_name: str, value: dict[str, object]) -> None:
        assert profile_name == "expert-125"
        document.clear()
        document.update(yaml.safe_load(yaml.safe_dump(value)))
        writes.append((profile_name, value))

    router.bind_profile_config_io(reader=read, writer=write)

    await router.prepare(prepared)

    provider_name = router.provider_name(prepared)
    assert provider_name in document["providers"]
    assert writes[-1][0] == "expert-125"
    assert not (configured.HERMES_HOME / "profiles" / "expert-125").exists()

    await router.close_session(prepared)

    assert "providers" not in document
    assert len(writes) == 2


@pytest.mark.asyncio
async def test_default_profile_keeps_supervisor_owned_filesystem_config(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    path = configured.HERMES_HOME / "config.yaml"
    path.write_text(
        yaml.safe_dump({"model": {"default": "17"}, "providers": {}}),
        encoding="utf-8",
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())

    async def unexpected_read(_profile_name: str) -> dict[str, object]:
        raise AssertionError("default Profile must not use Dashboard config IO")

    async def unexpected_write(
        _profile_name: str, _value: dict[str, object]
    ) -> None:
        raise AssertionError("default Profile must not use Dashboard config IO")

    router.bind_profile_config_io(
        reader=unexpected_read,
        writer=unexpected_write,
    )

    await router.prepare(identity())

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert router.provider_name(identity()) in document["providers"]


@pytest.mark.asyncio
async def test_personal_post_turn_route_is_exact_and_activates_only_at_terminal(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path).model_copy(
        update={"RUNTIME_MODE": "personal"}
    )
    configured.HERMES_HOME.mkdir(parents=True)
    path = configured.HERMES_HOME / "config.yaml"
    path.write_text(
        yaml.safe_dump({"model": {"default": "17"}, "providers": {}}),
        encoding="utf-8",
    )
    registry = ModelRouteRegistry()
    router = SessionModelRouter(configured, registry)
    prepared = identity()
    run_id = "run-post-turn-0001"

    await router.prepare(prepared)
    await router.activate(prepared, run_id)

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    provider_name = router.post_turn_provider_name(prepared, run_id)
    assert document["auxiliary"]["background_review"] == {
        "provider": f"custom:{provider_name}",
        "model": "17",
    }
    assert (
        document["providers"][provider_name]["base_url"]
        .endswith(
            f"/model-relay/"
            f"{router.post_turn_route_token(prepared, run_id)}/v1"
        )
    )

    pending = asyncio.create_task(
        registry.get(router.post_turn_route_token(prepared, run_id))
    )
    await asyncio.sleep(0)
    assert not pending.done()

    await router.finish_run(prepared, run_id, completed=True)
    route = await pending
    assert route.run_id == run_id
    assert route.authorization_scope == "post_turn_review"
    assert route.max_output_tokens == 8192
    with pytest.raises(ModelRelayRequestRejected):
        await registry.get(router.route_token(prepared))

    await router.close()
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert provider_name not in document.get("providers", {})
    assert router.provider_name(prepared) in document["providers"]
    assert document["auxiliary"]["background_review"]["provider"] == "auto"


def test_route_token_changes_with_session_or_model(tmp_path: Path) -> None:
    router = SessionModelRouter(settings(tmp_path), ModelRouteRegistry())
    prepared = identity()

    assert router.route_token(prepared) == router.route_token(prepared)
    assert router.route_token(prepared) != router.route_token(
        replace(prepared, session_id="session-99999999")
    )
    assert router.route_token(prepared) != router.route_token(
        replace(prepared, model_id="18")
    )


@pytest.mark.asyncio
async def test_reasoning_required_uses_upstream_model_for_hermes_but_keeps_platform_id(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    path = configured.HERMES_HOME / "config.yaml"
    path.write_text(yaml.safe_dump({"providers": {}}), encoding="utf-8")
    router = SessionModelRouter(configured, ModelRouteRegistry())
    prepared = replace(
        identity(),
        model_id="19",
        upstream_model_id="deepseek-v4-flash",
        runtime_capabilities={
            "reasoning_replay": "required",
            "thinking_mode": "enabled",
        },
    )

    await router.prepare(prepared)
    await router.activate(prepared, "run-reasoning-0001")

    provider = yaml.safe_load(path.read_text(encoding="utf-8"))["providers"][
        router.provider_name(prepared)
    ]
    assert provider["default_model"] == "deepseek-v4-flash"
    assert set(provider["models"]) == {"deepseek-v4-flash"}
    assert provider["extra_body"] == {"thinking": {"type": "enabled"}}
    route = await router.registry.get(router.route_token(prepared))
    assert route.model_id == "19"
    assert route.hermes_model_id == "deepseek-v4-flash"
    assert route.reasoning_replay == "required"


@pytest.mark.asyncio
async def test_session_route_uses_decrypted_per_model_provider(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    configured.HERMES_HOME.mkdir(parents=True)
    (configured.HERMES_HOME / "config.yaml").write_text(
        yaml.safe_dump({"providers": {}}), encoding="utf-8"
    )
    router = SessionModelRouter(
        configured,
        ModelRouteRegistry(),
        provider_route_codec=_ProviderRouteCodec(),
        provider_route_control_keys={3: b"r" * 32},
        provider_route_binding=_provider_binding(),
    )
    warmups: list[tuple[str, str]] = []
    router.bind_provider_route_warmup(
        lambda base_url, api_key: warmups.append((base_url, api_key))
    )
    digest = "sha256:" + "1" * 64
    prepared = replace(
        identity(),
        model_id="21",
        upstream_model_id="qwen3.7-plus",
        provider_route={
            "version": 1,
            "capsule": "prc1.3.nonce.ciphertext",
            "digest": digest,
        },
        provider_route_digest=digest,
    )

    await router.prepare(prepared)
    assert warmups == [
        (
            "https://coding.dashscope.aliyuncs.com/v1",
            "dashscope-secret-value",
        )
    ]
    await router.activate(prepared, "run-provider-route-0001")
    assert warmups == [
        (
            "https://coding.dashscope.aliyuncs.com/v1",
            "dashscope-secret-value",
        )
    ]
    route = await router.registry.get(router.route_token(prepared))
    assert route.provider_base_url == (
        "https://coding.dashscope.aliyuncs.com/v1"
    )
    assert route.provider_api_key.get_secret_value() == "dashscope-secret-value"
    assert "dashscope-secret-value" not in repr(route)


@pytest.mark.asyncio
async def test_required_provider_route_rejects_legacy_identity(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path).model_copy(
        update={"PROVIDER_ROUTE_ACCEPTANCE_MODE": "required"}
    )
    configured.HERMES_HOME.mkdir(parents=True)
    (configured.HERMES_HOME / "config.yaml").write_text(
        yaml.safe_dump({"providers": {}}), encoding="utf-8"
    )
    router = SessionModelRouter(configured, ModelRouteRegistry())

    with pytest.raises(RuntimeError, match="PROVIDER_ROUTE_REQUIRED"):
        await router.prepare(identity())
