from __future__ import annotations

from datetime import datetime
import asyncio

import pytest

from app.schemas.personal_knowledge import RuntimePersonalKnowledgeSearchRequest
from app.services.personal_knowledge_provider import PersonalKnowledgeProviderError
from app.services.runtime_personal_knowledge import (
    EffectivePersonalKnowledge,
    EffectivePersonalKnowledgeFile,
    RuntimePersonalKnowledgeError,
    RuntimePersonalKnowledgeIdentity,
    RuntimePersonalKnowledgeService,
)


def _knowledge(
    knowledge_base_id: int,
    dataset_id: str,
    *,
    provider: str,
    capabilities: dict | None,
) -> EffectivePersonalKnowledge:
    return EffectivePersonalKnowledge(
        knowledge_base_id=knowledge_base_id,
        name=f"知识库 {knowledge_base_id}",
        description="案例",
        dataset_id=dataset_id,
        updated_at=datetime(2026, 11, 2, 8, 0, knowledge_base_id),
        files=(
            EffectivePersonalKnowledgeFile(
                file_id=knowledge_base_id * 10,
                filename=f"文档 {knowledge_base_id}.pdf",
                oss_url=None,
                oss_key=None,
                provider_document_id=f"doc-{knowledge_base_id}",
            ),
        ),
        provider=provider,
        provider_capabilities=capabilities,
    )


def _mixed_knowledge():
    return [
        _knowledge(1, "fb-1", provider="filebase", capabilities=None),
        _knowledge(
            2,
            "wk-wiki",
            provider="weknora",
            capabilities={"vector": False, "keyword": False, "wiki": True},
        ),
        _knowledge(
            3,
            "wk-rag",
            provider="weknora",
            capabilities={"vector": True, "keyword": False, "wiki": False},
        ),
    ]


class FakeStore:
    def __init__(self, knowledge_bases, *, denial_code: str | None = None):
        self.knowledge_bases = knowledge_bases
        self.denial_code = denial_code
        self.effective_calls = 0

    async def authenticate(self, *_args):
        return RuntimePersonalKnowledgeIdentity(
            1,
            2,
            7,
            11,
            True,
            search_denial_code=self.denial_code,
        )

    async def effective(self, _identity):
        self.effective_calls += 1
        return self.knowledge_bases


class FakeFilebase:
    def __init__(self, chunks=None, *, delay: bool = False):
        self.chunks = chunks or []
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(0)
            return {"data": {"chunks": self.chunks}}
        finally:
            self.active -= 1


class FakeWeKnora:
    def __init__(
        self,
        *,
        rag=None,
        wiki_hits=None,
        rag_error: Exception | None = None,
        delay: bool = False,
    ):
        self.delay = delay
        self.rag = rag or []
        self.wiki_hits = wiki_hits or []
        self.rag_error = rag_error
        self.rag_calls = []
        self.wiki_calls = []
        self.active = 0
        self.max_active = 0

    async def _call(self):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(0)
        finally:
            self.active -= 1

    async def search(self, **kwargs):
        self.rag_calls.append(kwargs)
        await self._call()
        if self.rag_error is not None:
            raise self.rag_error
        return {"success": True, "data": self.rag}

    async def search_wiki(self, **kwargs):
        self.wiki_calls.append(kwargs)
        await self._call()
        return {"success": True, "data": self.wiki_hits}


def _request(query: str = "部署", **kwargs):
    return RuntimePersonalKnowledgeSearchRequest(query=query, **kwargs)


def _service(store, filebase, weknora, *, rerank=None):
    return RuntimePersonalKnowledgeService(
        store,
        provider=filebase,
        weknora_provider=weknora,
        weknora_rerank_enabled=rerank,
    )


async def test_hybrid_search_splits_capabilities_and_maps_platform_sources() -> None:
    filebase = FakeFilebase(
        [
            {
                "id": "fb-chunk",
                "dataset_id": "fb-1",
                "document_keyword": "文档 1.pdf",
                "content": "Filebase 证据",
                "similarity": 0.92,
            }
        ]
    )
    weknora = FakeWeKnora(
        rag=[
            {
                "knowledge_base_id": "wk-rag",
                "knowledge_id": "doc-3",
                "content": "WeKnora 证据",
                "score": 0.91,
            }
        ],
        wiki_hits=[
            {
                "knowledge_base_id": "wk-wiki",
                "slug": "concept/deploy",
                "title": "部署",
                "summary": "部署概念",
                "source_refs": ["doc-2|wiki"],
            }
        ],
    )
    store = FakeStore(_mixed_knowledge())
    result = await _service(store, filebase, weknora).hybrid_search(
        7,
        11,
        "runtime-token",
        _request(),
    )

    assert [match.file_id for match in result.rag_results] == [10, 30]
    assert [match.knowledge_base_id for match in result.rag_results] == [1, 3]
    assert [match.slug for match in result.wiki_results] == ["concept/deploy"]
    assert [file.file_id for file in result.wiki_results[0].source_files] == [20]
    assert result.rag_searched_knowledge_base_ids == [1, 3]
    assert result.wiki_searched_knowledge_base_ids == [2]
    assert result.low_confidence is False
    assert store.effective_calls == 1
    assert weknora.rag_calls == [
        {
            "question": "部署",
            "dataset_ids": ["wk-rag"],
            "match_count": 10,
            "rerank_enabled": True,
        }
    ]
    assert weknora.wiki_calls == [
        {"query": "部署", "dataset_ids": ["wk-wiki"], "limit": 10}
    ]


async def test_hybrid_search_runs_rag_and_wiki_concurrently() -> None:
    filebase = FakeFilebase(delay=True)
    weknora = FakeWeKnora(delay=True)
    await _service(
        FakeStore(_mixed_knowledge()),
        filebase,
        weknora,
    ).hybrid_search(7, 11, "runtime-token", _request())

    assert weknora.max_active == 2


async def test_hybrid_requested_ids_only_narrow_capability_scope() -> None:
    filebase = FakeFilebase()
    weknora = FakeWeKnora()
    result = await _service(
        FakeStore(_mixed_knowledge()),
        filebase,
        weknora,
    ).hybrid_search(
        7,
        11,
        "runtime-token",
        _request(knowledge_base_ids=[1, 3]),
    )

    assert result.rag_searched_knowledge_base_ids == [1, 3]
    assert result.wiki_results == []
    assert result.wiki_searched_knowledge_base_ids == []
    assert weknora.wiki_calls == []

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await _service(
            FakeStore(_mixed_knowledge()),
            filebase,
            weknora,
        ).hybrid_search(
            7,
            11,
            "runtime-token",
            _request(knowledge_base_ids=[999]),
        )

    assert exc.value.code == "KNOWLEDGE_BASE_NO_PERMISSION"
    assert exc.value.status_code == 403


async def test_hybrid_search_fails_closed_when_either_retrieval_fails() -> None:
    weknora = FakeWeKnora(
        rag_error=PersonalKnowledgeProviderError(
            "knowledge_search_failed",
            status_code=500,
        )
    )
    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await _service(
            FakeStore(_mixed_knowledge()),
            FakeFilebase(),
            weknora,
        ).hybrid_search(7, 11, "runtime-token", _request())

    assert exc.value.code == "personal_knowledge_search_unavailable"
    assert exc.value.status_code == 503


async def test_hybrid_rerank_setting_only_applies_to_rag() -> None:
    async def rerank_disabled() -> bool:
        return False

    weknora = FakeWeKnora()
    await _service(
        FakeStore(_mixed_knowledge()),
        FakeFilebase(),
        weknora,
        rerank=rerank_disabled,
    ).hybrid_search(7, 11, "runtime-token", _request())

    assert weknora.rag_calls[0]["rerank_enabled"] is False
    assert "rerank_enabled" not in weknora.wiki_calls[0]
