from __future__ import annotations

import json

import httpx
import pytest

from app.services.personal_knowledge_provider import (
    PersonalKnowledgeProviderError,
    WeKnoraPersonalKnowledgeClient,
)


def _client(handler) -> tuple[WeKnoraPersonalKnowledgeClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = WeKnoraPersonalKnowledgeClient(
        api_base_url="http://weknora.example/api/v1",
        api_key="test-key",
        client=http,
    )
    return subject, http


async def test_weknora_wiki_search_uses_native_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "upstream-page-id",
                        "knowledge_base_id": "kb-1",
                        "slug": "concept/rag",
                        "title": "RAG",
                        "summary": "检索增强生成",
                        "match_snippet": "部署",
                    }
                ],
            },
        )

    subject, http = _client(handler)
    try:
        body = await subject.search_wiki(
            query="部署|发布",
            dataset_ids=["kb-1"],
        )
    finally:
        await http.aclose()

    assert captured == {
        "method": "POST",
        "url": "http://weknora.example/api/v1/wiki-search",
        "body": {
            "query": "部署|发布",
            "knowledge_base_ids": ["kb-1"],
            "limit": 10,
        },
    }
    assert body["data"][0]["slug"] == "concept/rag"


async def test_weknora_read_wiki_page_encodes_slug_by_segment() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "slug": "concept/rag 指南",
                    "title": "RAG 指南",
                    "content": "完整正文",
                    "source_refs": ["doc-1|制度.pdf"],
                },
            },
        )

    subject, http = _client(handler)
    try:
        body = await subject.read_wiki_page(
            dataset_id="kb-1",
            slug="concept/rag 指南",
        )
    finally:
        await http.aclose()

    assert captured == {
        "method": "GET",
        "url": (
            "http://weknora.example/api/v1/knowledgebase/kb-1/wiki/pages/"
            "concept/rag%20%E6%8C%87%E5%8D%97"
        ),
    }
    assert body["data"]["content"] == "完整正文"


async def test_weknora_read_wiki_page_accepts_native_top_level_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "knowledge_base_id": "kb-1",
                "slug": "concept/rag",
                "title": "RAG",
                "content": "原生顶层正文",
                "source_refs": ["doc-1|制度.pdf"],
            },
        )

    subject, http = _client(handler)
    try:
        body = await subject.read_wiki_page(dataset_id="kb-1", slug="concept/rag")
    finally:
        await http.aclose()

    assert body["data"]["content"] == "原生顶层正文"


async def test_weknora_read_wiki_page_rejects_error_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "message": "Wiki page unavailable"},
        )

    subject, http = _client(handler)
    try:
        with pytest.raises(PersonalKnowledgeProviderError) as exc:
            await subject.read_wiki_page(dataset_id="kb-1", slug="concept/rag")
    finally:
        await http.aclose()

    assert exc.value.code == "read_wiki_page_failed"


@pytest.mark.parametrize(
    "slug",
    ["../secret", "/absolute", "concept/../../secret", "concept//bad", "a\\b"],
)
async def test_weknora_read_wiki_page_rejects_unsafe_slug(slug: str) -> None:
    subject, http = _client(
        lambda _request: pytest.fail("unsafe slug must not reach WeKnora")
    )
    try:
        with pytest.raises(PersonalKnowledgeProviderError) as exc:
            await subject.read_wiki_page(dataset_id="kb-1", slug=slug)
    finally:
        await http.aclose()

    assert exc.value.code == "read_wiki_page_failed"
    assert exc.value.status_code == 400


async def test_weknora_read_wiki_page_preserves_not_found_status() -> None:
    subject, http = _client(
        lambda _request: httpx.Response(
            404,
            json={"success": False, "message": "Wiki page not found"},
        )
    )
    try:
        with pytest.raises(PersonalKnowledgeProviderError) as exc:
            await subject.read_wiki_page(dataset_id="kb-1", slug="missing")
    finally:
        await http.aclose()

    assert exc.value.code == "read_wiki_page_failed"
    assert exc.value.status_code == 404


async def test_weknora_wiki_search_rejects_non_list_data() -> None:
    subject, http = _client(
        lambda _request: httpx.Response(
            200,
            json={"success": True, "data": {"items": []}},
        )
    )
    try:
        with pytest.raises(PersonalKnowledgeProviderError) as exc:
            await subject.search_wiki(query="部署", dataset_ids=["kb-1"])
    finally:
        await http.aclose()

    assert exc.value.code == "wiki_search_failed"
