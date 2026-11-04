from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.services.weknora_rerank_governance import (
    WeKnoraRerankGovernanceError,
    WeKnoraRerankGovernanceService,
)


MODELS_PAYLOAD = {
    "success": True,
    "data": [
        {
            "id": "chat-1",
            "name": "deepseek",
            "type": "KnowledgeQA",
            "status": "active",
            "parameters": {"base_url": "http://internal", "api_key": "secret"},
        },
        {
            "id": "inactive-rerank",
            "name": "inactive-rerank",
            "type": "Rerank",
            "status": "download_failed",
        },
        {
            "id": "rerank-b",
            "name": "rerank-b",
            "display_name": "B Rerank",
            "type": "Rerank",
            "status": "active",
            "source": "remote",
        },
        {
            "id": "rerank-a",
            "name": "rerank-a",
            "display_name": "A Rerank",
            "type": "Rerank",
            "status": "active",
            "source": "remote",
        },
    ],
}


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_get_config_reports_active_models_and_auto_selection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "test-key"
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=MODELS_PAYLOAD)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "embedding_top_k": 50,
                    "vector_threshold": 0.15,
                    "keyword_threshold": 0.3,
                    "rerank_top_k": 10,
                    "rerank_threshold": 0.2,
                    "rerank_model_id": "",
                },
            },
        )

    http = client(handler)
    service = WeKnoraRerankGovernanceService(
        api_base_url="http://weknora.test/api/v1",
        api_key="test-key",
        client=http,
    )
    try:
        snapshot = await service.get_config()
    finally:
        await http.aclose()

    assert [model.id for model in snapshot.models] == ["rerank-a", "rerank-b"]
    assert snapshot.configured_model_id == ""
    assert snapshot.effective_model_id == "rerank-a"
    assert snapshot.config_source == "auto"


@pytest.mark.asyncio
async def test_update_config_validates_model_and_preserves_other_fields() -> None:
    updates: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=MODELS_PAYLOAD)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "embedding_top_k": 77,
                        "vector_threshold": 0.21,
                        "keyword_threshold": 0.32,
                        "rerank_top_k": 12,
                        "rerank_threshold": 0.25,
                        "rerank_model_id": "rerank-a",
                        "rrf_k": 80,
                    },
                },
            )
        assert request.method == "PUT"
        updates.append((request.url.path, json.loads(request.content.decode())))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    **updates[0][1],
                    "rerank_model_id": "rerank-b",
                },
            },
        )

    http = client(handler)
    service = WeKnoraRerankGovernanceService(
        api_base_url="http://weknora.test/api/v1",
        api_key="test-key",
        client=http,
    )
    try:
        snapshot = await service.update_config(rerank_model_id="rerank-b")
    finally:
        await http.aclose()

    assert updates[0][0] == "/api/v1/tenants/kv/retrieval-config"
    assert updates[0][1]["embedding_top_k"] == 77
    assert updates[0][1]["vector_threshold"] == 0.21
    assert updates[0][1]["rrf_k"] == 80
    assert updates[0][1]["rerank_model_id"] == "rerank-b"
    assert snapshot.config_source == "tenant"
    assert snapshot.effective_model_id == "rerank-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["inactive-rerank", "chat-1", "missing"])
async def test_update_config_rejects_unusable_model(model_id: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MODELS_PAYLOAD)

    http = client(handler)
    service = WeKnoraRerankGovernanceService(
        api_base_url="http://weknora.test/api/v1",
        api_key="test-key",
        client=http,
    )
    try:
        with pytest.raises(WeKnoraRerankGovernanceError) as exc:
            await service.update_config(rerank_model_id=model_id)
    finally:
        await http.aclose()

    assert exc.value.code == "weknora_rerank_model_invalid"
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 500])
async def test_upstream_rejection_maps_to_unavailable(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"success": False})

    http = client(handler)
    service = WeKnoraRerankGovernanceService(
        api_base_url="http://weknora.test/api/v1",
        api_key="test-key",
        client=http,
    )
    try:
        with pytest.raises(WeKnoraRerankGovernanceError) as exc:
            await service.get_config()
    finally:
        await http.aclose()

    assert exc.value.code == "weknora_rerank_config_unavailable"
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_missing_provider_settings_reports_disabled() -> None:
    http = client(lambda _request: httpx.Response(200, json={}))
    service = WeKnoraRerankGovernanceService(
        api_base_url="",
        api_key="",
        client=http,
    )
    try:
        with pytest.raises(WeKnoraRerankGovernanceError) as exc:
            await service.get_config()
    finally:
        await http.aclose()

    assert exc.value.code == "weknora_rerank_config_unavailable"
    assert exc.value.status_code == 503
