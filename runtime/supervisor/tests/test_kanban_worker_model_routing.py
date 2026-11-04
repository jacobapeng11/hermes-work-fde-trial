from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from platform_supervisor.model_relay import (
    KanbanWorkerModelIdentity,
    KanbanWorkerModelRouter,
    ModelRelayRequestRejected,
    ModelRouteRegistry,
)
from platform_supervisor.provider_routes import (
    ProviderRouteBindingIdentity,
    ProviderRouteMaterial,
)
from platform_supervisor.settings import SupervisorSettings


def _settings(tmp_path: Path) -> SupervisorSettings:
    return SupervisorSettings(
        _env_file=None,
        RUNTIME_ID="runtime-personal-115",
        RUNTIME_BINDING_ID="binding-193",
        RUNTIME_MODE="personal",
            HERMES_STARTUP_MODE="steady_state",
        API_SERVER_KEY="runtime-only-secret-0123456789abcdef",
        NEWAPI_API_KEY="model-gateway-only-secret-0123456789",
        NEWAPI_BASE_URL="https://newapi.example/v1",
        HERMES_HOME=tmp_path / "hermes",
    )


def _identity(*, run_id: str = "kw-115-task-00000001") -> KanbanWorkerModelIdentity:
    return KanbanWorkerModelIdentity(
        run_id=run_id,
        task_id="t_task0001",
        board="default",
        profile_id="expert-75",
        model_id="1",
        upstream_model_id="claude-sonnet-4-5",
        runtime_capabilities={"thinking_mode": "enabled"},
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
            provider_route_digest="sha256:" + "2" * 64,
        )


def _provider_binding() -> ProviderRouteBindingIdentity:
    return ProviderRouteBindingIdentity(
        runtime_id="runtime-personal-115",
        binding_type="personal_runtime",
        binding_id=193,
        pvc_storage_identity_hash="sha256:" + "b" * 64,
    )


@pytest.mark.asyncio
async def test_worker_route_is_profile_stable_task_bound_and_repairs_provider(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path)
    profile_home = configured.HERMES_HOME / "profiles" / "expert-75"
    profile_home.mkdir(parents=True)
    path = profile_home / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "custom:runtime-stale",
                    "default": "old-model",
                },
                "providers": {
                    "platform": {
                        "base_url": "http://127.0.0.1:9121/model-relay/stale/v1"
                    },
                    "runtime-stale": {"base_url": "http://stale.invalid/v1"},
                },
            }
        ),
        encoding="utf-8",
    )
    registry = ModelRouteRegistry()
    router = KanbanWorkerModelRouter(configured, registry)
    identity = _identity()

    result = await router.activate(identity)

    assert result["route_token"] == router.route_token("expert-75")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["model"]["provider"] == "custom:platform"
    assert document["model"]["default"] == "claude-sonnet-4-5"
    provider = document["providers"]["platform"]
    assert "runtime-stale" in document["providers"]
    assert provider["base_url"].endswith(
        f"/model-relay/{router.route_token('expert-75')}/v1"
    )
    assert provider["default_model"] == "claude-sonnet-4-5"
    assert provider["extra_body"] == {"thinking": {"type": "enabled"}}
    route = await registry.get(router.route_token("expert-75"))
    assert route.run_id == identity.run_id
    assert route.session_id == "kanban:default:t_task0001"
    assert route.authorization_scope == "kanban_worker"
    assert route.model_id == "1"
    assert route.hermes_model_id == "claude-sonnet-4-5"

    await router.activate(identity)
    await router.revoke(identity)
    with pytest.raises(ModelRelayRequestRejected):
        await registry.get(router.route_token("expert-75"))


@pytest.mark.asyncio
async def test_worker_route_rejects_cross_run_takeover_and_late_revoke(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path)
    profile_home = configured.HERMES_HOME / "profiles" / "expert-75"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {}, "providers": {}}),
        encoding="utf-8",
    )
    registry = ModelRouteRegistry()
    router = KanbanWorkerModelRouter(configured, registry)
    first = _identity(run_id="kw-115-task-00000001")
    second = _identity(run_id="kw-115-task-00000002")

    await router.activate(first)
    with pytest.raises(ModelRelayRequestRejected, match="another Run"):
        await router.activate(second)

    await router.revoke(second)
    assert (await registry.get(router.route_token("expert-75"))).run_id == first.run_id


def test_worker_route_token_is_isolated_by_profile(tmp_path: Path) -> None:
    router = KanbanWorkerModelRouter(_settings(tmp_path), ModelRouteRegistry())

    assert router.route_token("expert-75") == router.route_token("expert-75")
    assert router.route_token("expert-75") != router.route_token("expert-59")
    with pytest.raises(ValueError, match="expert Profile"):
        router.route_token("../../default")


@pytest.mark.asyncio
async def test_worker_route_uses_decrypted_per_model_provider(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path)
    profile_home = configured.HERMES_HOME / "profiles" / "expert-75"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {}, "providers": {}}), encoding="utf-8"
    )
    router = KanbanWorkerModelRouter(
        configured,
        ModelRouteRegistry(),
        provider_route_codec=_ProviderRouteCodec(),
        provider_route_control_keys={3: b"r" * 32},
        provider_route_binding=_provider_binding(),
    )
    digest = "sha256:" + "2" * 64
    routed = KanbanWorkerModelIdentity(
        run_id="kw-115-task-00000003",
        task_id="t_task0003",
        board="default",
        profile_id="expert-75",
        model_id="21",
        upstream_model_id="qwen3.7-plus",
        runtime_capabilities={},
        max_output_tokens=8192,
        org_id=1,
        provider_route={
            "version": 1,
            "capsule": "prc1.3.nonce.ciphertext",
            "digest": digest,
        },
        provider_route_digest=digest,
    )

    await router.activate(routed)

    route = await router.registry.get(router.route_token("expert-75"))
    assert route.provider_base_url == (
        "https://coding.dashscope.aliyuncs.com/v1"
    )
    assert route.provider_api_key.get_secret_value() == "dashscope-secret-value"


@pytest.mark.asyncio
async def test_required_worker_route_rejects_legacy_identity(
    tmp_path: Path,
) -> None:
    configured = _settings(tmp_path).model_copy(
        update={"PROVIDER_ROUTE_ACCEPTANCE_MODE": "required"}
    )
    router = KanbanWorkerModelRouter(configured, ModelRouteRegistry())

    with pytest.raises(RuntimeError, match="PROVIDER_ROUTE_REQUIRED"):
        await router.activate(_identity())
