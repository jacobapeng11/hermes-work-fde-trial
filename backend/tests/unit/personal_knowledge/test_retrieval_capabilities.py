from __future__ import annotations

from datetime import datetime

import httpx

from app.schemas.personal_knowledge import RuntimePersonalKnowledgeState
from app.services.personal_knowledge_provider import (
    PersonalKnowledgeProviderError,
    WeKnoraPersonalKnowledgeClient,
)
from app.services.runtime_personal_knowledge import (
    EffectivePersonalKnowledge,
    EffectivePersonalKnowledgeFile,
    RuntimePersonalKnowledgeIdentity,
    RuntimePersonalKnowledgeService,
)
from scripts.migrate_personal_knowledge_weknora_capabilities import (
    capability_backfill,
    planned_statements,
)


def test_capability_snapshot_model_and_repeat_safe_migration_plan() -> None:
    assert planned_statements(columns=set()) == [
        "ALTER TABLE knowledge_base ADD COLUMN provider_capabilities JSON NULL",
        "ALTER TABLE knowledge_base "
        "ADD COLUMN provider_capabilities_synced_at DATETIME NULL",
    ]
    assert planned_statements(
        columns={"provider_capabilities", "provider_capabilities_synced_at"}
    ) == []


def test_capability_backfill_only_matches_exact_dataset_ids() -> None:
    updates = capability_backfill(
        knowledge_bases=[
            {"id": 1, "dataset_id": "wk-1"},
            {"id": 2, "dataset_id": "missing"},
            {"id": 3, "dataset_id": "invalid"},
        ],
        datasets=[
            {
                "id": "wk-1",
                "capabilities": {
                    "vector": True,
                    "keyword": False,
                    "wiki": True,
                    "graph": False,
                    "faq": False,
                },
            },
            {"id": "invalid", "capabilities": {"vector": "yes"}},
        ],
    )

    assert updates == [
        {
            "knowledge_base_id": 1,
            "capabilities": {
                "vector": True,
                "keyword": False,
                "wiki": True,
                "graph": False,
            },
        }
    ]
    assert "name" not in str(updates)
    assert "content" not in str(updates)


async def test_weknora_list_datasets_returns_capability_views() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers["X-API-Key"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": "wk-1",
                        "capabilities": {
                            "vector": True,
                            "keyword": True,
                            "wiki": True,
                            "graph": False,
                        },
                    }
                ],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = WeKnoraPersonalKnowledgeClient(
        api_base_url="http://weknora.example/api/v1",
        api_key="test-key",
        client=http,
    )
    try:
        result = await subject.list_datasets()
    finally:
        await http.aclose()

    assert captured == {
        "method": "GET",
        "url": "http://weknora.example/api/v1/knowledge-bases",
        "api_key": "test-key",
    }
    assert result[0]["capabilities"]["wiki"] is True


async def test_weknora_list_datasets_rejects_non_list_data() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"success": True, "data": {"items": []}},
            )
        )
    )
    subject = WeKnoraPersonalKnowledgeClient(
        api_base_url="http://weknora.example/api/v1",
        api_key="test-key",
        client=http,
    )
    try:
        try:
            await subject.list_datasets()
        except PersonalKnowledgeProviderError as exc:
            assert exc.code == "list_datasets_failed"
        else:
            raise AssertionError("non-list data must fail closed")
    finally:
        await http.aclose()


class FakeStore:
    def __init__(self, knowledge_bases):
        self.knowledge_bases = knowledge_bases

    async def authenticate(self, runtime_workspace_id, runtime_agent_profile_id, token):
        return RuntimePersonalKnowledgeIdentity(1, 2, 7, 11, True)

    async def effective(self, identity):
        return self.knowledge_bases


def _knowledge(capabilities: dict | None, *, provider: str = "weknora"):
    return EffectivePersonalKnowledge(
        knowledge_base_id=2,
        name="知识库 2",
        description="案例",
        dataset_id="wk-secret",
        updated_at=datetime(2026, 10, 30, 8, 0, 2),
        files=(
            EffectivePersonalKnowledgeFile(
                file_id=22,
                filename="文档.pdf",
                oss_url=None,
                oss_key=None,
                provider_document_id="doc-22",
            ),
        ),
        provider=provider,
        provider_capabilities=capabilities,
    )


def _service(knowledge_base) -> RuntimePersonalKnowledgeService:
    def forbidden_factory():
        raise AssertionError("state must not construct or call WeKnora")

    return RuntimePersonalKnowledgeService(
        FakeStore([knowledge_base]),
        provider=object(),
        weknora_provider_factory=forbidden_factory,
    )


async def test_state_exposes_capabilities_and_revision_tracks_changes() -> None:
    wiki_enabled = await _service(
        _knowledge({"vector": True, "keyword": False, "wiki": True, "graph": False})
    ).state(7, 11, "runtime-token")
    wiki_disabled = await _service(
        _knowledge({"vector": True, "keyword": False, "wiki": False, "graph": False})
    ).state(7, 11, "runtime-token")

    assert wiki_enabled.knowledge_bases[0].capabilities.rag is True
    assert wiki_enabled.knowledge_bases[0].capabilities.wiki is True
    assert wiki_disabled.knowledge_bases[0].capabilities.wiki is False
    assert wiki_enabled.revision != wiki_disabled.revision
    assert "wk-secret" not in wiki_enabled.model_dump_json()


async def test_state_defaults_unknown_weknora_and_filebase_capabilities() -> None:
    unknown = await _service(_knowledge(None)).state(7, 11, "runtime-token")
    filebase = await _service(
        _knowledge({"wiki": True}, provider="filebase")
    ).state(7, 11, "runtime-token")

    assert unknown.knowledge_bases[0].capabilities.rag is True
    assert unknown.knowledge_bases[0].capabilities.wiki is False
    assert filebase.knowledge_bases[0].capabilities.rag is True
    assert filebase.knowledge_bases[0].capabilities.wiki is False


def test_legacy_cached_state_defaults_to_rag_only() -> None:
    state = RuntimePersonalKnowledgeState.model_validate(
        {
            "enabled": True,
            "knowledge_bases": [
                {
                    "id": 2,
                    "name": "旧缓存",
                    "description": None,
                    "ready_file_count": 1,
                }
            ],
            "revision": "pkb:v2:7:11:legacy",
        }
    )

    assert state.knowledge_bases[0].capabilities.rag is True
    assert state.knowledge_bases[0].capabilities.wiki is False
