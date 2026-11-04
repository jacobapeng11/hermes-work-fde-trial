from __future__ import annotations

from datetime import datetime

import pytest

from app.schemas.personal_knowledge import (
    RuntimePersonalKnowledgeSearchRequest,
    RuntimePersonalKnowledgeWikiReadPageRequest,
)
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
    files: tuple[EffectivePersonalKnowledgeFile, ...] | None = None,
) -> EffectivePersonalKnowledge:
    return EffectivePersonalKnowledge(
        knowledge_base_id=knowledge_base_id,
        name=f"知识库 {knowledge_base_id}",
        description="案例",
        dataset_id=dataset_id,
        updated_at=datetime(2026, 10, 30, 8, 0, knowledge_base_id),
        files=files
        or (
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


class FakeStore:
    def __init__(self, knowledge_bases, *, denial_code: str | None = None):
        self.knowledge_bases = knowledge_bases
        self.denial_code = denial_code

    async def authenticate(self, runtime_workspace_id, runtime_agent_profile_id, token):
        return RuntimePersonalKnowledgeIdentity(
            1,
            2,
            7,
            11,
            True,
            search_denial_code=self.denial_code,
        )

    async def effective(self, identity):
        return self.knowledge_bases


class FakeFilebase:
    def __init__(self):
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"data": {"chunks": []}}


class FakeWeKnora:
    def __init__(self, *, wiki_hits=None, page=None, rag=None):
        self.wiki_hits = wiki_hits if wiki_hits is not None else []
        self.page = page or {}
        self.rag = rag or []
        self.rag_calls = []
        self.wiki_calls = []
        self.page_calls = []

    async def search(self, **kwargs):
        self.rag_calls.append(kwargs)
        return {"success": True, "data": self.rag}

    async def search_wiki(self, **kwargs):
        self.wiki_calls.append(kwargs)
        return {"success": True, "data": self.wiki_hits}

    async def read_wiki_page(self, **kwargs):
        self.page_calls.append(kwargs)
        if isinstance(self.page, Exception):
            raise self.page
        return {"success": True, "data": self.page}


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


async def test_rag_scope_excludes_wiki_only_and_rejects_explicit_unsupported() -> None:
    filebase = FakeFilebase()
    weknora = FakeWeKnora()
    subject = RuntimePersonalKnowledgeService(
        FakeStore(_mixed_knowledge()),
        provider=filebase,
        weknora_provider=weknora,
    )

    result = await subject.search(
        7,
        11,
        "runtime-token",
        RuntimePersonalKnowledgeSearchRequest(query="问题"),
    )

    assert result.searched_knowledge_base_ids == [1, 3]
    assert filebase.calls == [{"question": "问题", "dataset_ids": ["fb-1"]}]
    assert weknora.rag_calls == [
        {
            "question": "问题",
            "dataset_ids": ["wk-rag"],
            "match_count": 10,
            "rerank_enabled": True,
        }
    ]
    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.search(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeSearchRequest(
                query="问题",
                knowledge_base_ids=[2],
            ),
        )
    assert exc.value.code == "personal_knowledge_capability_unavailable"
    assert exc.value.status_code == 409


async def test_wiki_scope_excludes_rag_only_and_rejects_explicit_unsupported() -> None:
    filebase = FakeFilebase()
    weknora = FakeWeKnora()
    subject = RuntimePersonalKnowledgeService(
        FakeStore(_mixed_knowledge()),
        provider=filebase,
        weknora_provider=weknora,
    )

    result = await subject.wiki_search(
        7,
        11,
        "runtime-token",
        RuntimePersonalKnowledgeSearchRequest(query="概念"),
    )

    assert result.matches == []
    assert result.low_confidence is True
    assert result.searched_knowledge_base_ids == [2]
    assert weknora.wiki_calls == [
        {"query": "概念", "dataset_ids": ["wk-wiki"], "limit": 10}
    ]
    assert weknora.rag_calls == []
    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.wiki_search(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeSearchRequest(
                query="概念",
                knowledge_base_ids=[3],
            ),
        )
    assert exc.value.code == "personal_knowledge_capability_unavailable"
    assert exc.value.status_code == 409


async def test_wiki_search_returns_page_hits_without_fake_file_ids() -> None:
    weknora = FakeWeKnora(
        wiki_hits=[
            {
                "id": "upstream-page-id",
                "knowledge_base_id": "wk-wiki",
                "slug": "concept/rag",
                "title": "RAG",
                "summary": "概念摘要",
                "match_snippet": "部署片段",
            }
        ]
    )
    subject = RuntimePersonalKnowledgeService(
        FakeStore(_mixed_knowledge()),
        provider=FakeFilebase(),
        weknora_provider=weknora,
    )

    result = await subject.wiki_search(
        7,
        11,
        "runtime-token",
        RuntimePersonalKnowledgeSearchRequest(query="部署"),
    )

    match = result.matches[0]
    assert match.source_type == "wiki_page"
    assert match.knowledge_base_id == 2
    assert match.slug == "concept/rag"
    assert match.summary == "概念摘要"
    assert match.match_snippet == "部署片段"
    assert match.source_files == []
    assert match.source_url == "/?view=knowledge&tab=personal&kb=2"
    assert "content" not in match.model_dump()
    assert "upstream-page-id" not in result.model_dump_json()
    assert "wk-wiki" not in result.model_dump_json()


async def test_wiki_read_page_returns_content_and_maps_multiple_source_files() -> None:
    files = (
        EffectivePersonalKnowledgeFile(
            file_id=11,
            filename="制度 A.pdf",
            oss_url=None,
            oss_key=None,
            provider_document_id="doc-a",
        ),
        EffectivePersonalKnowledgeFile(
            file_id=22,
            filename="制度 B.pdf",
            oss_url=None,
            oss_key=None,
            provider_document_id="doc-b",
        ),
    )
    knowledge = _knowledge(
        2,
        "wk-wiki",
        provider="weknora",
        capabilities={"vector": False, "keyword": False, "wiki": True},
        files=files,
    )
    weknora = FakeWeKnora(
        page={
            "id": "upstream-page-id",
            "knowledge_base_id": "wk-wiki",
            "slug": "concept/rag",
            "title": "RAG",
            "content": "完整 Wiki 正文",
            "source_refs": ["doc-a|A", "doc-b|B", "unknown|Unknown"],
            "chunk_refs": ["upstream-chunk-id"],
        }
    )
    subject = RuntimePersonalKnowledgeService(
        FakeStore([knowledge]),
        provider=FakeFilebase(),
        weknora_provider=weknora,
    )

    result = await subject.wiki_read_page(
        7,
        11,
        "runtime-token",
        RuntimePersonalKnowledgeWikiReadPageRequest(
            knowledge_base_id=2,
            slug="concept/rag",
        ),
    )

    assert weknora.page_calls == [
        {"dataset_id": "wk-wiki", "slug": "concept/rag"}
    ]
    assert result.source_type == "wiki_page"
    assert result.content == "完整 Wiki 正文"
    assert [file.file_id for file in result.source_files] == [11, 22]
    assert result.source_files[0].source_url.endswith("kb=2&file=11")
    payload = result.model_dump_json()
    assert "upstream" not in payload
    assert "doc-a" not in payload
    assert "doc-b" not in payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("knowledge_base_id", "other-kb"),
        ("slug", "other-page"),
        ("knowledge_base_id", None),
        ("slug", None),
    ],
)
async def test_wiki_read_page_rejects_upstream_identity_mismatch(
    field: str,
    value: object,
) -> None:
    page = {
        "knowledge_base_id": "wk-wiki",
        "slug": "concept/rag",
        "title": "RAG",
        "content": "不应接受的正文",
        field: value,
    }
    weknora = FakeWeKnora(page=page)
    subject = RuntimePersonalKnowledgeService(
        FakeStore(_mixed_knowledge()),
        provider=FakeFilebase(),
        weknora_provider=weknora,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.wiki_read_page(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeWikiReadPageRequest(
                knowledge_base_id=2,
                slug="concept/rag",
            ),
        )

    assert exc.value.code == "personal_knowledge_search_unavailable"
    assert exc.value.status_code == 503


async def test_wiki_read_page_rejects_unsafe_slug_before_provider_call() -> None:
    weknora = FakeWeKnora()
    subject = RuntimePersonalKnowledgeService(
        FakeStore(_mixed_knowledge()),
        provider=FakeFilebase(),
        weknora_provider=weknora,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.wiki_read_page(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeWikiReadPageRequest(
                knowledge_base_id=2,
                slug="../secret",
            ),
        )

    assert exc.value.code == "personal_knowledge_wiki_page_not_found"
    assert exc.value.status_code == 404
    assert weknora.page_calls == []


async def test_wiki_read_page_maps_upstream_not_found() -> None:
    weknora = FakeWeKnora(
        page=PersonalKnowledgeProviderError(
            "read_wiki_page_failed",
            status_code=404,
        )
    )
    subject = RuntimePersonalKnowledgeService(
        FakeStore(_mixed_knowledge()),
        provider=FakeFilebase(),
        weknora_provider=weknora,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.wiki_read_page(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeWikiReadPageRequest(
                knowledge_base_id=2,
                slug="missing",
            ),
        )

    assert exc.value.code == "personal_knowledge_wiki_page_not_found"
    assert exc.value.status_code == 404


async def test_wiki_calls_preserve_application_service_denial() -> None:
    weknora = FakeWeKnora()
    subject = RuntimePersonalKnowledgeService(
        FakeStore([], denial_code="knowledge_user_delegation_required"),
        provider=FakeFilebase(),
        weknora_provider=weknora,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.wiki_search(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeSearchRequest(query="概念"),
        )

    assert exc.value.code == "knowledge_user_delegation_required"
    assert exc.value.status_code == 403
    assert weknora.wiki_calls == []
