from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.im_gateway.coordinator import ImGatewayCoordinator


def test_coordinator_health_is_explicit_when_feature_is_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "IM_GATEWAY_ENABLED", False)

    health = ImGatewayCoordinator().health()

    assert health["status"] == "disabled"
    assert health["enabled"] is False
    assert health["coordinator_alive"] is False


@pytest.mark.asyncio
async def test_coordinator_recovers_after_a_reconciliation_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "IM_GATEWAY_ENABLED", True)
    monkeypatch.setattr(
        settings, "IM_GATEWAY_RECONCILE_INTERVAL_SECONDS", 0.01
    )
    calls = 0
    recovered = asyncio.Event()

    async def reconcile() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        recovered.set()

    coordinator = ImGatewayCoordinator()
    coordinator.configure(reconcile=reconcile)
    coordinator.start()
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2)
        health = coordinator.health()
        assert health["status"] == "ok"
        assert health["coordinator_alive"] is True
        assert health["consecutive_failures"] == 0
        assert health["last_success_at"] is not None
        assert health["last_error_code"] is None
    finally:
        await coordinator.stop()
