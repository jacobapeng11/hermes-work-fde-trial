from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from platform_supervisor.execution import PrepareIdentity
from platform_supervisor.hermes.dashboard_protocol import (
    _public_chat_error_payload,
)
from platform_supervisor.model_relay import (
    ModelRelayRequestRejected,
    ModelRouteRegistry,
    SessionModelRouter,
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
        NEWAPI_API_KEY="model-gateway-only-secret-0123456789abcdef",
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


@pytest.mark.asyncio
async def test_registry_renews_only_the_exact_active_run_route(tmp_path: Path) -> None:
    registry = ModelRouteRegistry()
    router = SessionModelRouter(
        settings(tmp_path),
        registry,
        route_renewal_interval_seconds=0.01,
    )
    prepared = identity()

    await router.activate(prepared, "run-1")
    token = router.route_token(prepared)
    before = await registry.get(token)

    renewed = await registry.renew_active_run_route(
        token,
        "run-1",
        ttl_seconds=3900,
    )
    after = await registry.get(token)

    assert renewed is True
    assert after.expires_at > before.expires_at

    assert await registry.renew_active_run_route(
        token,
        "run-other",
        ttl_seconds=3900,
    ) is False
    await router.close()


@pytest.mark.asyncio
async def test_active_run_route_is_renewed_while_running(tmp_path: Path) -> None:
    registry = ModelRouteRegistry()
    router = SessionModelRouter(
        settings(tmp_path),
        registry,
        route_renewal_interval_seconds=0.01,
    )
    prepared = identity()

    await router.activate(prepared, "run-1")
    token = router.route_token(prepared)
    before = await registry.get(token)
    await asyncio.sleep(0.05)
    after = await registry.get(token)

    assert after.expires_at > before.expires_at
    assert after.run_id == "run-1"
    await router.close()


@pytest.mark.asyncio
async def test_terminal_run_stops_renewal_and_revokes_route(tmp_path: Path) -> None:
    registry = ModelRouteRegistry()
    router = SessionModelRouter(
        settings(tmp_path),
        registry,
        route_renewal_interval_seconds=0.01,
    )
    prepared = identity()

    await router.activate(prepared, "run-1")
    token = router.route_token(prepared)
    await router.finish_run(prepared, "run-1", completed=True)

    with pytest.raises(ModelRelayRequestRejected):
        await registry.get(token)
    renewal = router._renewal_tasks.get("run-1")
    assert renewal is None or renewal.done()
    await router.close()


@pytest.mark.asyncio
async def test_revoke_run_stops_renewal_and_removes_route(tmp_path: Path) -> None:
    registry = ModelRouteRegistry()
    router = SessionModelRouter(
        settings(tmp_path),
        registry,
        route_renewal_interval_seconds=0.01,
    )
    prepared = identity()

    await router.activate(prepared, "run-1")
    token = router.route_token(prepared)
    await router.revoke_run("run-1")

    with pytest.raises(ModelRelayRequestRejected):
        await registry.get(token)
    renewal = router._renewal_tasks.get("run-1")
    assert renewal is None or renewal.done()
    await router.close()


@pytest.mark.asyncio
async def test_non_active_scope_route_is_not_renewed() -> None:
    from platform_supervisor.model_relay.relay import ActiveModelRoute

    registry = ModelRouteRegistry()
    base = ActiveModelRoute(
        route_token="a" * 40,
        run_id="run-review",
        session_id="session-1",
        model_id="17",
        api_mode="openai",
        max_output_tokens=1024,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        authorization_scope="post_turn_review",
    )
    await registry.register(base)

    assert await registry.renew_active_run_route(
        "a" * 40,
        "run-review",
        ttl_seconds=3900,
    ) is False
    route = await registry.get("a" * 40, touch_post_turn=False)
    assert route.expires_at == base.expires_at


def test_model_route_expiry_error_is_translated_for_users() -> None:
    payload = _public_chat_error_payload(
        'HTTP 403: {"detail":"model relay route has expired"}'
    )
    assert payload == {
        "code": "MODEL_ROUTE_EXPIRED",
        "message": "任务执行时间过长，模型调用授权已过期，请重新发送任务",
    }

    assert _public_chat_error_payload("upstream timeout") == {
        "message": "upstream timeout"
    }
