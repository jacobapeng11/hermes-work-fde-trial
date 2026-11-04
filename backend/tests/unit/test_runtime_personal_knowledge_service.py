from datetime import datetime
from types import SimpleNamespace

import pytest

from app.schemas.personal_knowledge import RuntimePersonalKnowledgeSearchRequest
from app.services.personal_knowledge_provider import PersonalKnowledgeProviderError
from app.services.runtime_personal_knowledge import (
    EffectivePersonalKnowledge,
    EffectivePersonalKnowledgeFile,
    RuntimePersonalKnowledgeError,
    RuntimePersonalKnowledgeIdentity,
    RuntimePersonalKnowledgeService,
    SqlAlchemyRuntimePersonalKnowledgeStore,
)


def _knowledge(
    knowledge_base_id: int,
    dataset_id: str,
) -> EffectivePersonalKnowledge:
    return EffectivePersonalKnowledge(
        knowledge_base_id=knowledge_base_id,
        name=f"知识库 {knowledge_base_id}",
        description="案例",
        dataset_id=dataset_id,
        updated_at=datetime(2026, 7, 20, 8, 0, knowledge_base_id),
        files=(
            EffectivePersonalKnowledgeFile(
                file_id=knowledge_base_id * 10,
                filename=f"文档 {knowledge_base_id}.pdf",
                oss_url=f"https://oss.example/{knowledge_base_id}.pdf",
                oss_key=f"knowledge/{knowledge_base_id}.pdf",
            ),
        ),
    )


class FakeStore:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.authenticate_calls = 0
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
        self.authenticate_calls += 1
        return RuntimePersonalKnowledgeIdentity(1, 2, 7, 11, True)

    async def effective(self, identity):
        assert identity == RuntimePersonalKnowledgeIdentity(1, 2, 7, 11, True)
        index = min(self.effective_calls, len(self.snapshots) - 1)
        self.effective_calls += 1
        return self.snapshots[index]


class FakeProvider:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeScalarResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return iter(self.values)


class FakeSqlSession:
    def __init__(self, result_values):
        self.result_values = list(result_values)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeScalarResult(self.result_values.pop(0))


class FakeBatchAcl:
    def __init__(self, roles):
        self.roles = roles
        self.calls = []

    async def effective_roles(self, **kwargs):
        self.calls.append(kwargs)
        return self.roles

    async def effective_role(self, **_kwargs):
        raise AssertionError("Personal Agent store must use the batch ACL path")


async def test_state_contains_platform_ids_but_never_dataset_ids() -> None:
    subject = RuntimePersonalKnowledgeService(
        FakeStore([[_knowledge(2, "ds-secret")]]),
        provider=FakeProvider({}),
    )

    state = await subject.state(7, 11, "runtime-token")
    payload = state.model_dump_json()

    assert state.enabled is True
    assert state.knowledge_bases[0].id == 2
    assert state.knowledge_bases[0].ready_file_count == 1
    assert state.revision.startswith("pkb:v2:7:11:")
    assert len(state.revision.rsplit(":", 1)[-1]) == 64
    assert "dataset" not in payload
    assert "ds-secret" not in payload


async def test_state_revision_is_order_independent_and_tracks_files() -> None:
    first = _knowledge(2, "ds-secret")
    second = _knowledge(3, "ds-other")
    subject = RuntimePersonalKnowledgeService(
        FakeStore([[second, first], [first, second]]),
        provider=FakeProvider({}),
    )

    first_state = await subject.state(7, 11, "runtime-token")
    second_state = await subject.state(7, 11, "runtime-token")

    assert first_state.revision == second_state.revision
    assert [item.id for item in first_state.knowledge_bases] == [2, 3]


async def test_search_rechecks_effective_acl_on_every_call() -> None:
    provider = FakeProvider({"data": {"chunks": []}})
    store = FakeStore([[_knowledge(2, "ds-2")], []])
    subject = RuntimePersonalKnowledgeService(
        store,
        provider=provider,
    )

    assert (await subject.state(7, 11, "runtime-token")).enabled is True
    result = await subject.search(
        7,
        11,
        "runtime-token",
        RuntimePersonalKnowledgeSearchRequest(query="案例"),
    )

    assert result.matches == []
    assert result.searched_knowledge_base_ids == []
    assert provider.calls == []
    assert store.effective_calls == 2


async def test_requested_platform_ids_only_narrow_effective_datasets() -> None:
    provider = FakeProvider(
        {
            "data": {
                "chunks": [
                    {
                        "id": "chunk-1",
                        "dataset_id": "ds-3",
                        "document_keyword": "文档 3.pdf",
                        "content": "命中的案例内容",
                        "similarity": 0.86,
                    }
                ]
            }
        }
    )
    subject = RuntimePersonalKnowledgeService(
        FakeStore([[_knowledge(2, "ds-2"), _knowledge(3, "ds-3")]]),
        provider=provider,
        min_similarity=0.5,
    )

    with pytest.raises(
        RuntimePersonalKnowledgeError,
        match="KNOWLEDGE_BASE_NO_PERMISSION",
    ):
        await subject.search(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeSearchRequest(
                query="案例",
                knowledge_base_ids=[3, 99],
            ),
        )
    assert provider.calls == []


async def test_low_similarity_and_unknown_files_are_dropped() -> None:
    provider = FakeProvider(
        {
            "data": {
                "chunks": [
                    {
                        "dataset_id": "ds-2",
                        "document_keyword": "文档 2.pdf",
                        "content": "低分",
                        "similarity": 0.2,
                    },
                    {
                        "dataset_id": "ds-2",
                        "document_keyword": "已删除.pdf",
                        "content": "不可见",
                        "similarity": 0.9,
                    },
                ]
            }
        }
    )
    subject = RuntimePersonalKnowledgeService(
        FakeStore([[_knowledge(2, "ds-2")]]),
        provider=provider,
        min_similarity=0.5,
    )

    result = await subject.search(
        7,
        11,
        "runtime-token",
        RuntimePersonalKnowledgeSearchRequest(query="案例"),
    )

    assert result.matches == []
    assert result.low_confidence is True


async def test_provider_failure_maps_to_service_unavailable() -> None:
    class FailingProvider:
        async def search(self, **_kwargs):
            raise PersonalKnowledgeProviderError("search_failed")

    subject = RuntimePersonalKnowledgeService(
        FakeStore([[_knowledge(2, "ds-2")]]),
        provider=FailingProvider(),
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await subject.search(
            7,
            11,
            "runtime-token",
            RuntimePersonalKnowledgeSearchRequest(query="案例"),
        )

    assert exc.value.code == "personal_knowledge_search_unavailable"
    assert exc.value.status_code == 503


async def test_personal_agent_store_batches_acl_and_ready_file_queries() -> None:
    knowledge_bases = [
        SimpleNamespace(
            id=2,
            name="知识库 2",
            description="有权限且有文件",
            dataset_id="ds-2",
            updated_at=datetime(2026, 7, 20, 8, 0, 2),
        ),
        SimpleNamespace(
            id=3,
            name="知识库 3",
            description="无权限",
            dataset_id="ds-3",
            updated_at=datetime(2026, 7, 20, 8, 0, 3),
        ),
        SimpleNamespace(
            id=4,
            name="知识库 4",
            description="有权限但无文件",
            dataset_id="ds-4",
            updated_at=datetime(2026, 7, 20, 8, 0, 4),
        ),
    ]
    files = [
        SimpleNamespace(
            id=22,
            knowledge_base_id=2,
            filename="文档 2-B.pdf",
            oss_url="https://oss.example/2-b.pdf",
            oss_key=None,
        ),
        SimpleNamespace(
            id=21,
            knowledge_base_id=2,
            filename="文档 2-A.pdf",
            oss_url="https://oss.example/2-a.pdf",
            oss_key="knowledge/2-a.pdf",
        ),
    ]
    session = FakeSqlSession([knowledge_bases, files])
    acl = FakeBatchAcl({2: "preview", 4: "retrieve"})
    store = SqlAlchemyRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        acl=acl,  # type: ignore[arg-type]
    )
    identity = RuntimePersonalKnowledgeIdentity(1, 8, 7, 11, True)

    effective = await store.effective(identity)

    assert [item.knowledge_base_id for item in effective] == [2]
    assert [file.file_id for file in effective[0].files] == [22, 21]
    assert effective[0].files[0].oss_key is None
    assert len(acl.calls) == 1
    assert acl.calls[0]["knowledge_bases"] == knowledge_bases
    assert len(session.statements) == 2

    file_statement = session.statements[1].compile()
    assert any(
        isinstance(value, list) and set(value) == {2, 4}
        for value in file_statement.params.values()
    )
    file_sql = str(file_statement)
    assert "knowledge_base_file.kb_id IN" in file_sql
    assert "knowledge_base_file.file_status" in file_sql
    assert "knowledge_base_file.is_deleted" in file_sql


async def test_personal_store_preserves_weknora_provider_and_file_mapping() -> None:
    knowledge_bases = [
        SimpleNamespace(
            id=2,
            name="WeKnora 制度库",
            description="案例",
            dataset_id="wk-2",
            updated_at=datetime(2026, 10, 30, 8, 0, 2),
            provider="weknora",
            provider_capabilities={
                "vector": True,
                "keyword": False,
                "wiki": True,
                "graph": False,
            },
        )
    ]
    files = [
        SimpleNamespace(
            id=22,
            knowledge_base_id=2,
            filename="制度.pdf",
            oss_url=None,
            oss_key=None,
            provider_document_id="doc-22",
        )
    ]

    class EffectiveStore:
        async def authenticate(self, *_args):
            return RuntimePersonalKnowledgeIdentity(1, 8, 7, 11, True)

        async def effective(self, identity):
            fresh_store = SqlAlchemyRuntimePersonalKnowledgeStore(
                FakeSqlSession([knowledge_bases, files]),  # type: ignore[arg-type]
                acl=FakeBatchAcl({2: "retrieve"}),  # type: ignore[arg-type]
            )
            return await fresh_store.effective(identity)

    weknora = FakeProvider(
        {
            "success": True,
            "data": [
                {
                    "id": "upstream-chunk",
                    "knowledge_base_id": "wk-2",
                    "knowledge_id": "doc-22",
                    "content": "WeKnora 证据",
                    "score": 0.8,
                }
            ],
        }
    )
    filebase = FakeProvider({"data": {"chunks": []}})
    subject = RuntimePersonalKnowledgeService(
        EffectiveStore(),
        provider=filebase,
        weknora_provider=weknora,
    )

    state = await subject.state(7, 11, "runtime-token")
    result = await subject.search(
        7,
        11,
        "runtime-token",
        RuntimePersonalKnowledgeSearchRequest(query="制度"),
    )

    assert state.knowledge_bases[0].capabilities.rag is True
    assert state.knowledge_bases[0].capabilities.wiki is True
    assert result.matches[0].file_id == 22
    assert result.matches[0].source_url.endswith("kb=2&file=22")
    assert weknora.calls == [
        {"question": "制度", "dataset_ids": ["wk-2"], "match_count": 10}
    ]
    assert filebase.calls == []


async def test_personal_agent_store_skips_file_query_without_effective_acl() -> None:
    knowledge_bases = [
        SimpleNamespace(
            id=2,
            name="知识库 2",
            description=None,
            dataset_id="ds-2",
            updated_at=datetime(2026, 7, 20, 8, 0, 2),
        )
    ]
    session = FakeSqlSession([knowledge_bases])
    acl = FakeBatchAcl({})
    store = SqlAlchemyRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        acl=acl,  # type: ignore[arg-type]
    )

    with pytest.raises(
        RuntimePersonalKnowledgeError,
        match="KNOWLEDGE_BASE_NO_PERMISSION",
    ):
        await store.effective(
            RuntimePersonalKnowledgeIdentity(1, 8, 7, 11, True)
        )
    assert len(acl.calls) == 1
    assert len(session.statements) == 1
