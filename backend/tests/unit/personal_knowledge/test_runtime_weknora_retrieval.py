from __future__ import annotations

from datetime import datetime

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
from app.services.weknora_rerank_settings import WeKnoraRerankSettingStoreError


def _knowledge(
    knowledge_base_id: int,
    dataset_id: str,
    *,
    provider: str,
    file_id: int,
    provider_document_id: str | None = None,
) -> EffectivePersonalKnowledge:
    return EffectivePersonalKnowledge(
        knowledge_base_id=knowledge_base_id,
        name=f"知识库 {knowledge_base_id}",
        description="案例",
        dataset_id=dataset_id,
        updated_at=datetime(2026, 10, 30, 8, 0, knowledge_base_id),
        files=(
            EffectivePersonalKnowledgeFile(
                file_id=file_id,
                filename=f"文档 {knowledge_base_id}.pdf",
                oss_url=None,
                oss_key=None,
                provider_document_id=provider_document_id,
            ),
        ),
        provider=provider,
    )


class FakeStore:
    def __init__(self, snapshots, *, denial_code: str | None = None):
        self.snapshots = list(snapshots)
        self.denial_code = denial_code
        self.effective_calls = 0

    async def authenticate(
        self,
        runtime_workspace_id,
        runtime_agent_profile_id,
        runtime_token,
    ):
        assert runtime_workspace_id == 7
        assert runtime_agent_profile_id == 11
        assert runtime_token == "runtime-token"
        return RuntimePersonalKnowledgeIdentity(
            1,
            2,
            7,
            11,
            True,
            search_denial_code=self.denial_code,
        )

    async def effective(self, identity):
        index = min(self.effective_calls, len(self.snapshots) - 1)
        self.effective_calls += 1
        return self.snapshots[index]


class FakeProvider:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _request(query: str = "问题", **kwargs) -> RuntimePersonalKnowledgeSearchRequest:
    return RuntimePersonalKnowledgeSearchRequest(query=query, **kwargs)


async def test_search_routes_providers_and_normalizes_weknora_citations() -> None:
    filebase = FakeProvider(
        {
            "data": {
                "chunks": [
                    {
                        "id": "fb-chunk",
                        "dataset_id": "fb-1",
                        "document_keyword": "文档 1.pdf",
                        "content": "Filebase 证据",
                        "similarity": 0.9,
                    }
                ]
            }
        }
    )
    weknora = FakeProvider(
        {
            "success": True,
            "data": [
                {
                    "id": "upstream-chunk-must-not-leak",
                    "knowledge_base_id": "wk-1",
                    "knowledge_id": "doc-22",
                    "content": "WeKnora 证据",
                    "score": 0.2,
                    "source_locators": [{"type": "pdf", "page": 3}],
                },
                {
                    "knowledge_base_id": "wk-1",
                    "knowledge_id": "unknown-doc",
                    "content": "未映射内容",
                    "score": 0.99,
                },
                {
                    "knowledge_base_id": "other-kb",
                    "knowledge_id": "doc-22",
                    "content": "越库内容",
                    "score": 0.99,
                },
                {
                    "knowledge_base_id": "wk-1",
                    "knowledge_id": "doc-22",
                    "content": " ",
                    "score": 0.99,
                },
            ],
        }
    )
    store = FakeStore(
        [
            [
                _knowledge(1, "fb-1", provider="filebase", file_id=11),
                _knowledge(
                    2,
                    "wk-1",
                    provider="weknora",
                    file_id=22,
                    provider_document_id="doc-22",
                ),
            ]
        ]
    )
    subject = RuntimePersonalKnowledgeService(
        store,
        provider=filebase,
        weknora_provider=weknora,
        min_similarity=0.5,
    )

    result = await subject.search(7, 11, "runtime-token", _request())

    assert filebase.calls == [{"question": "问题", "dataset_ids": ["fb-1"]}]
    assert weknora.calls == [
        {
            "question": "问题",
            "dataset_ids": ["wk-1"],
            "match_count": 10,
            "rerank_enabled": True,
        }
    ]
    assert [match.file_id for match in result.matches] == [11, 22]
    weknora_match = result.matches[1]
    assert weknora_match.knowledge_base_id == 2
    assert weknora_match.filename == "文档 2.pdf"
    assert weknora_match.chunk_id is None
    assert weknora_match.score == 0.2
    assert weknora_match.page == 3
    assert weknora_match.source_url == (
        "/?view=knowledge&tab=personal&kb=2&file=22"
    )
    assert "upstream-chunk-must-not-leak" not in result.model_dump_json()
    assert result.searched_knowledge_base_ids == [1, 2]
    assert result.low_confidence is False


async def test_requested_ids_still_fail_closed_before_any_provider_call() -> None:
    filebase = FakeProvider({"data": {"chunks": []}})
    weknora = FakeProvider({"success": True, "data": []})
    subject = RuntimePersonalKnowledgeService(
        FakeStore(
            [[_knowledge(2, "wk-1", provider="weknora", file_id=22)]]
        ),
        provider=filebase,
        weknora_provider=weknora,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.search(
            7,
            11,
            "runtime-token",
            _request(knowledge_base_ids=[2, 99]),
        )

    assert exc.value.code == "KNOWLEDGE_BASE_NO_PERMISSION"
    assert exc.value.status_code == 403
    assert filebase.calls == []
    assert weknora.calls == []


async def test_search_rechecks_acl_and_avoids_upstream_when_effective_empty() -> None:
    weknora = FakeProvider({"success": True, "data": []})
    store = FakeStore(
        [
            [_knowledge(2, "wk-1", provider="weknora", file_id=22)],
            [],
        ]
    )

    def forbidden_factory():
        raise AssertionError("empty effective scope must not construct WeKnora")

    subject = RuntimePersonalKnowledgeService(
        store,
        provider=FakeProvider({"data": {"chunks": []}}),
        weknora_provider=weknora,
        weknora_provider_factory=forbidden_factory,
    )

    await subject.search(7, 11, "runtime-token", _request())
    result = await subject.search(7, 11, "runtime-token", _request())

    assert result.matches == []
    assert result.searched_knowledge_base_ids == []
    assert store.effective_calls == 2
    assert weknora.calls == [
        {"question": "问题", "dataset_ids": ["wk-1"], "match_count": 10,
         "rerank_enabled": True}
    ]


async def test_search_can_disable_weknora_rerank() -> None:
    weknora = FakeProvider({"success": True, "data": []})
    subject = RuntimePersonalKnowledgeService(
        FakeStore([[_knowledge(2, "wk-1", provider="weknora", file_id=22)]]),
        provider=FakeProvider({"data": {"chunks": []}}),
        weknora_provider=weknora,
        weknora_rerank_enabled=lambda: _disabled_setting(),
    )

    await subject.search(7, 11, "runtime-token", _request())

    assert weknora.calls == [
        {
            "question": "问题",
            "dataset_ids": ["wk-1"],
            "match_count": 10,
            "rerank_enabled": False,
        }
    ]


async def test_search_fails_closed_when_rerank_policy_is_unavailable() -> None:
    weknora = FakeProvider({"success": True, "data": []})

    async def unavailable_setting() -> bool:
        raise WeKnoraRerankSettingStoreError

    subject = RuntimePersonalKnowledgeService(
        FakeStore([[_knowledge(2, "wk-1", provider="weknora", file_id=22)]]),
        provider=FakeProvider({"data": {"chunks": []}}),
        weknora_provider=weknora,
        weknora_rerank_enabled=unavailable_setting,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.search(7, 11, "runtime-token", _request())

    assert exc.value.code == "personal_knowledge_search_unavailable"
    assert exc.value.status_code == 503
    assert weknora.calls == []


async def _disabled_setting() -> bool:
    return False


@pytest.mark.parametrize("failing_provider", ["filebase", "weknora"])
async def test_any_provider_failure_fails_whole_mixed_search(
    failing_provider: str,
) -> None:
    error = PersonalKnowledgeProviderError("search_failed")
    filebase = FakeProvider(
        error if failing_provider == "filebase" else {"data": {"chunks": []}}
    )
    weknora = FakeProvider(
        error if failing_provider == "weknora" else {"success": True, "data": []}
    )
    subject = RuntimePersonalKnowledgeService(
        FakeStore(
            [
                [
                    _knowledge(1, "fb-1", provider="filebase", file_id=11),
                    _knowledge(2, "wk-1", provider="weknora", file_id=22),
                ]
            ]
        ),
        provider=filebase,
        weknora_provider=weknora,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.search(7, 11, "runtime-token", _request())

    assert exc.value.code == "personal_knowledge_search_unavailable"
    assert exc.value.status_code == 503


async def test_filebase_only_search_does_not_require_weknora_provider() -> None:
    filebase = FakeProvider({"data": {"chunks": []}})

    def forbidden_factory():
        raise AssertionError("Filebase-only search must not construct WeKnora")

    subject = RuntimePersonalKnowledgeService(
        FakeStore(
            [[_knowledge(1, "fb-1", provider="filebase", file_id=11)]]
        ),
        provider=filebase,
        weknora_provider_factory=forbidden_factory,
    )

    result = await subject.search(7, 11, "runtime-token", _request())

    assert result.matches == []
    assert filebase.calls == [{"question": "问题", "dataset_ids": ["fb-1"]}]


async def test_weknora_provider_is_constructed_lazily_on_first_use() -> None:
    weknora = FakeProvider({"success": True, "data": []})
    factory_calls = []

    def factory():
        factory_calls.append("called")
        return weknora

    subject = RuntimePersonalKnowledgeService(
        FakeStore(
            [[_knowledge(2, "wk-1", provider="weknora", file_id=22)]]
        ),
        provider=FakeProvider({"data": {"chunks": []}}),
        weknora_provider_factory=factory,
    )
    assert factory_calls == []

    await subject.search(7, 11, "runtime-token", _request())
    await subject.search(7, 11, "runtime-token", _request())

    assert factory_calls == ["called"]
    assert len(weknora.calls) == 2


async def test_application_service_denial_is_preserved_without_provider_calls() -> None:
    filebase = FakeProvider({"data": {"chunks": []}})
    weknora = FakeProvider({"success": True, "data": []})
    subject = RuntimePersonalKnowledgeService(
        FakeStore([[]], denial_code="knowledge_user_delegation_required"),
        provider=filebase,
        weknora_provider=weknora,
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.search(7, 11, "runtime-token", _request())

    assert exc.value.code == "knowledge_user_delegation_required"
    assert exc.value.status_code == 403
    assert filebase.calls == []
    assert weknora.calls == []
