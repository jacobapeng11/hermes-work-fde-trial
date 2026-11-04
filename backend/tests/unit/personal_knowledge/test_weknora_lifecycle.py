from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.schemas.personal_knowledge import PersonalKnowledgeCreate
from app.models import KnowledgeBase
from app.services import application_agent_personal_knowledge
from app.services.personal_knowledge import (
    PersonalKnowledgeError,
    PersonalKnowledgeService,
)
from app.services.personal_knowledge_provider import (
    PersonalKnowledgeProviderError,
    WeKnoraPersonalKnowledgeClient,
    build_personal_knowledge_provider,
)
from app.services.runtime_workspace import Principal


def _client(
    handler,
) -> tuple[WeKnoraPersonalKnowledgeClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = WeKnoraPersonalKnowledgeClient(
        api_base_url="http://weknora.example/api/v1",
        api_key="test-key",
        embedding_model_id="embedding-1",
        summary_model_id="summary-1",
        wiki_synthesis_model_id="wiki-summary-1",
        wiki_extraction_granularity="exhaustive",
        client=http,
    )
    return subject, http


async def test_weknora_create_uses_api_key_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-API-Key")
        captured["has_authorization"] = "Authorization" in request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"success": True, "data": {"id": "kb-1"}},
        )

    subject, http = _client(handler)
    try:
        assert await subject.create_dataset("案例库") == "kb-1"
    finally:
        await http.aclose()

    assert captured == {
        "method": "POST",
        "url": "http://weknora.example/api/v1/knowledge-bases",
        "api_key": "test-key",
        "has_authorization": False,
        "body": {
            "name": "案例库",
            "type": "document",
            "embedding_model_id": "embedding-1",
            "summary_model_id": "summary-1",
            "indexing_strategy": {
                "vector_enabled": True,
                "keyword_enabled": True,
                "wiki_enabled": True,
                "graph_enabled": False,
            },
            "wiki_config": {
                "synthesis_model_id": "wiki-summary-1",
                "extraction_granularity": "exhaustive",
            },
        },
    }


@pytest.mark.parametrize(
    ("model_argument", "setting_name", "expected_code"),
    [
        (
            "embedding_model_id",
            "WEKNORA_EMBEDDING_MODEL_ID",
            "weknora_embedding_model_not_configured",
        ),
        (
            "summary_model_id",
            "WEKNORA_SUMMARY_MODEL_ID",
            "weknora_summary_model_not_configured",
        ),
        (
            "wiki_synthesis_model_id",
            "WEKNORA_WIKI_SYNTHESIS_MODEL_ID",
            "weknora_wiki_synthesis_model_not_configured",
        ),
    ],
)
async def test_weknora_create_requires_models(
    monkeypatch: pytest.MonkeyPatch,
    model_argument: str,
    setting_name: str,
    expected_code: str,
) -> None:
    monkeypatch.setattr(
        f"app.services.personal_knowledge_provider.settings.{setting_name}",
        "",
    )
    model_ids = {
        "embedding_model_id": "embedding-1",
        "summary_model_id": "summary-1",
        "wiki_synthesis_model_id": "wiki-summary-1",
        model_argument: "",
    }
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("create must not call WeKnora without models")
        )
    )
    subject = WeKnoraPersonalKnowledgeClient(
        api_base_url="http://weknora.example/api/v1",
        api_key="test-key",
        client=http,
        **model_ids,
    )
    try:
        with pytest.raises(
            PersonalKnowledgeProviderError,
            match=expected_code,
        ):
            await subject.create_dataset("案例库")
    finally:
        await http.aclose()


async def test_weknora_delete_encodes_dataset_id() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"success": True})

    subject, http = _client(handler)
    try:
        await subject.delete_dataset("kb/需要编码")
    finally:
        await http.aclose()

    assert captured["method"] == "DELETE"
    assert captured["url"] == (
        "http://weknora.example/api/v1/knowledge-bases/"
        "kb%2F%E9%9C%80%E8%A6%81%E7%BC%96%E7%A0%81"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"success": False}),
        httpx.Response(403, json={"success": False}),
        httpx.Response(404, json={"success": False}),
        httpx.Response(500, text="unavailable"),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"success": False}),
        httpx.Response(200, json={"success": True, "data": {}}),
        httpx.Response(200, json={"success": True, "data": "invalid"}),
    ],
)
async def test_weknora_create_failures_are_normalized(
    response: httpx.Response,
) -> None:
    subject, http = _client(lambda _request: response)
    try:
        with pytest.raises(PersonalKnowledgeProviderError):
            await subject.create_dataset("案例库")
    finally:
        await http.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"success": False}),
        httpx.Response(403, json={"success": False}),
        httpx.Response(404, json={"success": False}),
        httpx.Response(500, text="unavailable"),
        httpx.Response(200, json={"success": False}),
    ],
)
async def test_weknora_delete_404_stays_failed(
    response: httpx.Response,
) -> None:
    subject, http = _client(lambda _request: response)
    try:
        with pytest.raises(
            PersonalKnowledgeProviderError,
            match="delete_dataset_failed",
        ):
            await subject.delete_dataset("kb-1")
    finally:
        await http.aclose()


def test_weknora_requires_managed_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.personal_knowledge_provider.settings.WEKNORA_API_BASE_URL", "")
    monkeypatch.setattr("app.services.personal_knowledge_provider.settings.WEKNORA_API_KEY", "")
    with pytest.raises(
        PersonalKnowledgeProviderError,
        match="weknora_provider_not_configured",
    ):
        WeKnoraPersonalKnowledgeClient(
            api_base_url="",
            api_key="",
        )


def test_unknown_provider_fails_closed() -> None:
    with pytest.raises(
        PersonalKnowledgeProviderError,
        match="personal_knowledge_provider_unsupported",
    ):
        build_personal_knowledge_provider("unknown")


class FakeLifecycleProvider:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []

    async def create_dataset(self, name: str) -> str:
        self.created.append(name)
        return f"external-{len(self.created)}"

    async def delete_dataset(self, dataset_id: str) -> None:
        self.deleted.append(dataset_id)


class FakeCreateSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.knowledge_bases: list[object] = []
        self.rolled_back = False

    def add(self, knowledge_base) -> None:
        knowledge_base.id = 501
        self.knowledge_bases.append(knowledge_base)

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("local commit failed")

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, _knowledge_base) -> None:
        return None


async def test_create_writes_selected_provider_and_external_id() -> None:
    provider = FakeLifecycleProvider()
    session = FakeCreateSession()
    service = PersonalKnowledgeService(
        session,
        providers={"weknora": provider},
        provider_name="weknora",
    )

    async def fake_detail(*, principal, knowledge_base_id):
        return SimpleNamespace(knowledge_base_id=knowledge_base_id)

    service.detail = fake_detail  # type: ignore[method-assign]

    await service.create(
        principal=Principal(org_id=1, user_id=2),
        command=PersonalKnowledgeCreate(name="WeKnora 案例库"),
    )

    knowledge_base = session.knowledge_bases[0]
    assert provider.created == ["WeKnora 案例库"]
    assert knowledge_base.provider == "weknora"
    assert knowledge_base.dataset_id == "external-1"
    assert knowledge_base.provider_capabilities == {
        "vector": True,
        "keyword": True,
        "wiki": True,
        "graph": False,
    }
    assert knowledge_base.provider_capabilities_synced_at is not None


async def test_local_create_failure_compensates_on_same_provider() -> None:
    provider = FakeLifecycleProvider()
    session = FakeCreateSession(fail_commit=True)
    service = PersonalKnowledgeService(
        session,
        providers={"weknora": provider},
        provider_name="weknora",
    )

    with pytest.raises(RuntimeError, match="local commit failed"):
        await service._create_owned(
            principal=Principal(org_id=1, user_id=2),
            command=PersonalKnowledgeCreate(name="WeKnora 案例库"),
            kind="personal",
            application_agent_id=None,
        )

    assert session.rolled_back is True
    assert provider.created == ["WeKnora 案例库"]
    assert provider.deleted == ["external-1"]


class FakeDeleteSession:
    def __init__(self) -> None:
        self.executed: list[object] = []
        self.deleted: list[object] = []
        self.commit_count = 0

    async def execute(self, statement) -> None:
        self.executed.append(statement)

    async def delete(self, knowledge_base) -> None:
        self.deleted.append(knowledge_base)

    async def commit(self) -> None:
        self.commit_count += 1


class OwnerAcl:
    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base

    async def accessible(self, **_kwargs):
        return self.knowledge_base, "owner"


async def test_delete_routes_by_row_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def revoke_bindings(_session, *, knowledge_base_id: int) -> None:
        assert knowledge_base_id == 501

    monkeypatch.setattr(
        application_agent_personal_knowledge,
        "revoke_bindings_for_deleted_knowledge",
        revoke_bindings,
    )

    for provider_name in ("filebase", "weknora"):
        filebase = FakeLifecycleProvider()
        weknora = FakeLifecycleProvider()
        knowledge_base = KnowledgeBase(
            id=501,
            org_id=1,
            owner_user_id=2,
            slug=f"personal-kb-{provider_name}",
            name="个人知识库",
            kind="personal",
            provider=provider_name,
            dataset_id=f"{provider_name}-501",
            status="active",
        )
        session = FakeDeleteSession()
        service = PersonalKnowledgeService(
            session,
            providers={"filebase": filebase, "weknora": weknora},
            acl=OwnerAcl(knowledge_base),  # type: ignore[arg-type]
        )

        async def remove_default_bindings(_knowledge_base) -> None:
            return None

        service._remove_default_profile_desired_bindings = (
            remove_default_bindings  # type: ignore[method-assign]
        )
        await service.delete(
            principal=Principal(org_id=1, user_id=2),
            knowledge_base_id=501,
        )

        assert filebase.deleted == (
            ["filebase-501"] if provider_name == "filebase" else []
        )
        assert weknora.deleted == (
            ["weknora-501"] if provider_name == "weknora" else []
        )
        assert session.deleted == [knowledge_base]
        assert session.commit_count == 1


async def test_delete_rejects_unknown_provider_without_local_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def revoke_bindings(_session, *, knowledge_base_id: int) -> None:
        raise AssertionError("revoke must not run")

    monkeypatch.setattr(
        application_agent_personal_knowledge,
        "revoke_bindings_for_deleted_knowledge",
        revoke_bindings,
    )
    knowledge_base = KnowledgeBase(
        id=501,
        org_id=1,
        owner_user_id=2,
        slug="personal-kb-unknown",
        name="个人知识库",
        kind="personal",
        provider="unknown",
        dataset_id="external-501",
        status="active",
    )
    session = FakeDeleteSession()
    service = PersonalKnowledgeService(
        session,
        providers={},
        acl=OwnerAcl(knowledge_base),  # type: ignore[arg-type]
    )

    with pytest.raises(PersonalKnowledgeError) as exc:
        await service.delete(
            principal=Principal(org_id=1, user_id=2),
            knowledge_base_id=501,
        )

    assert exc.value.code == "delete_dataset_failed"
    assert session.deleted == []
    assert session.commit_count == 0
