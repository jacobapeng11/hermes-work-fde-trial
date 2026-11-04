import json

import httpx
import pytest

from app.services.personal_knowledge_provider import (
    FilebasePersonalKnowledgeClient,
    PersonalKnowledgeProviderError,
)


def _client(
    handler,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[FilebasePersonalKnowledgeClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    return (
        FilebasePersonalKnowledgeClient(
            sync_base_url="https://filebase.example/sf/api/kb-file-sync",
            search_url="https://filebase.example/sf/api/search/agent-search",
            client=http,
        ),
        http,
    )


async def test_create_dataset_preserves_legacy_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["has_authorization"] = "Authorization" in request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"dataset_id": "ds-1"})

    subject, http = _client(
        handler,
        headers={"Authorization": "Bearer must-not-leak"},
    )
    try:
        assert await subject.create_dataset("案例库") == "ds-1"
    finally:
        await http.aclose()

    assert captured == {
        "method": "POST",
        "url": "https://filebase.example/sf/api/kb-file-sync/create-dataset",
        "has_authorization": False,
        "body": {"name": "案例库"},
    }


async def test_delete_dataset_preserves_legacy_delete_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["has_authorization"] = "Authorization" in request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": 1})

    subject, http = _client(
        handler,
        headers={"Authorization": "Bearer must-not-leak"},
    )
    try:
        await subject.delete_dataset("ds-1")
    finally:
        await http.aclose()

    assert captured == {
        "method": "DELETE",
        "has_authorization": False,
        "body": {"ids": ["ds-1"]},
    }


async def test_search_preserves_agent_search_contract_without_authorization() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["has_authorization"] = "Authorization" in request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"chunks": [{"content": "命中"}]}})

    subject, http = _client(
        handler,
        headers={"Authorization": "Bearer must-not-leak"},
    )
    try:
        result = await subject.search(
            question="园区网案例",
            dataset_ids=["ds-1", "ds-2"],
        )
    finally:
        await http.aclose()

    assert captured == {
        "has_authorization": False,
        "body": {"question": "园区网案例", "dataset_ids": ["ds-1", "ds-2"]},
    }
    assert result["data"]["chunks"][0]["content"] == "命中"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"error": "rejected"}),
    ],
)
async def test_create_dataset_normalizes_provider_failures(response: httpx.Response) -> None:
    subject, http = _client(lambda _request: response)
    try:
        with pytest.raises(PersonalKnowledgeProviderError, match="create_dataset_failed"):
            await subject.create_dataset("案例库")
    finally:
        await http.aclose()


async def test_delete_dataset_rejects_zero_deleted_count() -> None:
    subject, http = _client(
        lambda _request: httpx.Response(200, json={"ok": 0})
    )
    try:
        with pytest.raises(PersonalKnowledgeProviderError, match="delete_dataset_failed"):
            await subject.delete_dataset("ds-1")
    finally:
        await http.aclose()
