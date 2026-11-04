from __future__ import annotations

import httpx
import pytest

import app.main as main_module
from app.main import app
from app.services import platform_readiness


@pytest.mark.asyncio
async def test_platform_readiness_is_ready_when_all_components_are_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def database() -> dict[str, str]:
        return {"status": "ready", "detail": "SELECT 1 succeeded"}

    async def runtime() -> dict[str, object]:
        return {
            "status": "ready",
            "checks": {
                "kubernetes_api": {
                    "status": "ready",
                    "detail": "Kubernetes API is ready",
                }
            },
        }

    monkeypatch.setattr(platform_readiness, "check_database_readiness", database)
    monkeypatch.setattr(
        platform_readiness,
        "check_runtime_infrastructure_readiness",
        runtime,
    )

    payload = await platform_readiness.get_platform_readiness()

    assert payload["status"] == "ready"
    assert payload["components"]["database"]["status"] == "ready"
    assert payload["components"]["runtime_infrastructure"]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_component", ["database", "runtime"])
async def test_platform_readiness_is_not_ready_when_one_component_fails(
    monkeypatch: pytest.MonkeyPatch,
    failed_component: str,
) -> None:
    async def database() -> dict[str, str]:
        return {
            "status": "not_ready" if failed_component == "database" else "ready",
            "detail": "database result",
        }

    async def runtime() -> dict[str, str]:
        return {
            "status": "not_ready" if failed_component == "runtime" else "ready",
            "detail": "runtime result",
        }

    monkeypatch.setattr(platform_readiness, "check_database_readiness", database)
    monkeypatch.setattr(
        platform_readiness,
        "check_runtime_infrastructure_readiness",
        runtime,
    )

    payload = await platform_readiness.get_platform_readiness()

    assert payload["status"] == "not_ready"


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_readyz_returns_200_for_ready_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ready() -> dict[str, object]:
        return {
            "status": "ready",
            "components": {
                "database": {"status": "ready"},
                "runtime_infrastructure": {"status": "ready"},
            },
        }

    monkeypatch.setattr(main_module, "get_platform_readiness", ready)

    response = await _get("/api/v1/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_readyz_returns_503_without_leaking_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def not_ready() -> dict[str, object]:
        return {
            "status": "not_ready",
            "components": {
                "database": {
                    "status": "not_ready",
                    "detail": "database check failed: TimeoutError",
                },
                "runtime_infrastructure": {"status": "ready"},
            },
        }

    monkeypatch.setattr(main_module, "get_platform_readiness", not_ready)

    response = await _get("/api/v1/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert "password" not in response.text.lower()
    assert "token" not in response.text.lower()
    assert "secret" not in response.text.lower()


@pytest.mark.asyncio
async def test_health_remains_a_liveness_endpoint() -> None:
    response = await _get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
