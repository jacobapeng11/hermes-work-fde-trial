from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.services.personal_knowledge_provider import (
    PersonalKnowledgeProviderError,
    WeKnoraPersonalKnowledgeClient,
)


def _client(
    handler,
) -> tuple[WeKnoraPersonalKnowledgeClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = WeKnoraPersonalKnowledgeClient(
        api_base_url="http://weknora.example/api/v1",
        api_key="test-key",
        client=http,
    )
    return subject, http


async def test_weknora_search_uses_knowledge_search_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-API-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "chunk-1",
                        "knowledge_id": "doc-1",
                        "knowledge_base_id": "kb-1",
                        "content": "部署需要审批",
                        "score": 0.81,
                    }
                ],
            },
        )

    subject, http = _client(handler)
    try:
        body = await subject.search(
            question="部署要求",
            dataset_ids=["kb-1"],
        )
    finally:
        await http.aclose()

    assert captured == {
        "method": "POST",
        "url": "http://weknora.example/api/v1/knowledge-search",
        "api_key": "test-key",
        "body": {
            "query": "部署要求",
            "knowledge_base_ids": ["kb-1"],
            "match_count": 10,
        },
    }
    assert body["data"][0]["knowledge_id"] == "doc-1"


async def test_weknora_search_honors_match_count() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": []})

    subject, http = _client(handler)
    try:
        await subject.search(
            question="部署要求",
            dataset_ids=["kb-1"],
            match_count=7,
        )
    finally:
        await http.aclose()

    assert captured["body"]["match_count"] == 7


async def test_weknora_search_can_disable_rerank() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": []})

    subject, http = _client(handler)
    try:
        await subject.search(
            question="部署要求",
            dataset_ids=["kb-1"],
            rerank_enabled=False,
        )
    finally:
        await http.aclose()

    assert captured["body"]["rerank"] == {"enabled": False}


async def test_weknora_search_rejects_non_list_data() -> None:
    subject, http = _client(
        lambda _request: httpx.Response(
            200,
            json={"success": True, "data": {"chunks": []}},
        )
    )
    try:
        with pytest.raises(PersonalKnowledgeProviderError, match="^search_failed$"):
            await subject.search(question="部署要求", dataset_ids=["kb-1"])
    finally:
        await http.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"success": False, "message": "upstream error"}),
        httpx.Response(200, json={"success": False, "data": []}),
        httpx.Response(200, text="not-json"),
    ],
)
async def test_weknora_search_rejects_invalid_upstream_without_leaking_key(
    response: httpx.Response,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subject, http = _client(lambda _request: response)
    try:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(
                PersonalKnowledgeProviderError,
                match="^search_failed$",
            ) as captured:
                await subject.search(question="部署要求", dataset_ids=["kb-1"])
    finally:
        await http.aclose()

    assert "test-key" not in str(captured.value)
    assert "test-key" not in caplog.text
