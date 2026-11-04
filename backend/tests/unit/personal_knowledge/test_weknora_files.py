from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app.models import KnowledgeBase, KnowledgeBaseFile
from app.services.personal_knowledge import PersonalKnowledgeError
from app.services.personal_knowledge_files import (
    PersonalKnowledgeFileAccess,
    PersonalKnowledgeFileService,
)
from app.services.personal_knowledge_provider import (
    PersonalKnowledgeProviderError,
    WeKnoraKnowledgeDocument,
    WeKnoraPersonalKnowledgeClient,
)
from app.services.runtime_workspace import Principal


def _client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        WeKnoraPersonalKnowledgeClient(
            api_base_url="http://weknora.example/api/v1",
            api_key="test-key",
            embedding_model_id="embedding-1",
            client=http,
        ),
        http,
    )


async def test_weknora_upload_uses_multipart_and_returns_external_identity() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-API-Key")
        captured["has_authorization"] = "Authorization" in request.headers
        captured["content_type"] = request.headers.get("Content-Type")
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"id": "document-1", "parse_status": "pending"},
            },
        )

    subject, http = _client(handler)
    try:
        document = await subject.upload_document(
            dataset_id="kb-1",
            filename="说明.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            content=b"doc-content",
        )
    finally:
        await http.aclose()

    assert document == WeKnoraKnowledgeDocument("document-1", "pending")
    assert captured["method"] == "POST"
    assert captured["url"] == (
        "http://weknora.example/api/v1/knowledge-bases/kb-1/knowledge/file"
    )
    assert captured["api_key"] == "test-key"
    assert captured["has_authorization"] is False
    assert "multipart/form-data" in str(captured["content_type"])
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'name="fileName"' in body
    assert "说明.docx".encode() in body
    assert b'name="channel"' in body and b"hermes" in body
    assert b'name="file"' in body and b"doc-content" in body


async def test_weknora_document_status_delete_and_stream_contracts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/knowledge/doc-1"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"id": "doc-1", "parse_status": "completed"},
                },
            )
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={"success": True, "data": {"task_id": "task-1"}},
            )
        return httpx.Response(200, content=b"file-content")

    subject, http = _client(handler)
    try:
        document = await subject.get_document("doc-1")
        assert document.parse_status == "completed"
        assert await subject.delete_document("doc-1") == "task-1"
        async with subject.stream_document("doc-1", download=False) as response:
            assert await response.aread() == b"file-content"
        async with subject.stream_document("doc-1", download=True) as response:
            assert await response.aread() == b"file-content"
    finally:
        await http.aclose()

    assert requests[2].url.path == "/api/v1/knowledge/doc-1/preview"
    assert requests[3].url.path == "/api/v1/knowledge/doc-1/download"
    assert all(request.headers.get("X-API-Key") == "test-key" for request in requests)
    assert all("Authorization" not in request.headers for request in requests)


class FakeProvider:
    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.deleted: list[str] = []
        self.upload_error: PersonalKnowledgeProviderError | None = None
        self.delete_error: PersonalKnowledgeProviderError | None = None
        self.document = WeKnoraKnowledgeDocument("doc-1", "pending")
        self.stream_calls: list[tuple[str, bool]] = []

    async def upload_document(self, **kwargs):
        self.uploads.append(kwargs)
        if self.upload_error is not None:
            raise self.upload_error
        return self.document

    async def delete_document(self, document_id: str):
        self.deleted.append(document_id)
        if self.delete_error is not None:
            raise self.delete_error
        return "task-1"

    async def get_document(self, document_id: str):
        if self.delete_error is not None and self.delete_error.status_code == 404:
            raise self.delete_error
        return self.document

    @asynccontextmanager
    async def stream_document(self, document_id: str, *, download: bool):
        self.stream_calls.append((document_id, download))
        yield httpx.Response(200, content=b"stream-content")


class FakeOss:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def upload_bytes(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(url="http://oss.example/file", key="file-key")


class FakeSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.file: KnowledgeBaseFile | None = None
        self.commit_count = 0
        self.rolled_back = False

    def add(self, file: KnowledgeBaseFile) -> None:
        self.file = file
        self.file.id = 215

    async def commit(self) -> None:
        self.commit_count += 1
        if self.fail_commit:
            raise RuntimeError("local commit failed")

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, file: KnowledgeBaseFile) -> None:
        if file.updated_at is None:
            file.updated_at = datetime(2026, 10, 29, 12, 0, 0)

    async def get(self, _model, _user_id: int):
        return SimpleNamespace(name="测试用户")

    async def scalar(self, _statement):
        return self.file


class FakeAcl:
    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base

    async def accessible(self, **_kwargs):
        return self.knowledge_base, "owner"


def _knowledge_base(provider: str = "weknora") -> KnowledgeBase:
    return KnowledgeBase(
        id=83,
        org_id=1,
        owner_user_id=2,
        slug="kb-83",
        name="测试知识库",
        provider=provider,
        dataset_id="weknora-kb-83",
        status="active",
    )


def _file(**overrides) -> KnowledgeBaseFile:
    values = {
        "id": 215,
        "org_id": 1,
        "knowledge_base_id": 83,
        "filename": "说明.docx",
        "oss_url": None,
        "oss_key": None,
        "provider_document_id": "doc-1",
        "provider_status": "pending",
        "size_bytes": 12,
        "mime_type": "application/octet-stream",
        "file_status": 0,
        "uploader_id": 2,
        "updater_id": 2,
        "is_deleted": 0,
        "updated_at": datetime(2026, 10, 29, 11, 0, 0),
    }
    values.update(overrides)
    return KnowledgeBaseFile(**values)


def _service(
    session: FakeSession,
    provider: FakeProvider,
    oss: FakeOss,
    knowledge_base: KnowledgeBase,
) -> PersonalKnowledgeFileService:
    return PersonalKnowledgeFileService(
        session,  # type: ignore[arg-type]
        providers={"weknora": provider},  # type: ignore[dict-item]
        oss=oss,  # type: ignore[arg-type]
        acl=FakeAcl(knowledge_base),  # type: ignore[arg-type]
    )


async def test_weknora_upload_skips_oss_and_persists_external_identity() -> None:
    session = FakeSession()
    provider = FakeProvider()
    oss = FakeOss()
    service = _service(session, provider, oss, _knowledge_base())

    result = await service.upload_file(
        principal=Principal(org_id=1, user_id=2),
        knowledge_base_id=83,
        filename="说明.docx",
        content_type="application/octet-stream",
        content=b"doc-content",
    )

    assert result.file_id == 215
    assert result.status == "pending"
    assert oss.calls == []
    assert provider.uploads[0]["dataset_id"] == "weknora-kb-83"
    assert session.file is not None
    assert session.file.provider_document_id == "doc-1"
    assert session.file.provider_status == "pending"
    assert session.file.oss_url is None


async def test_weknora_upload_compensates_remote_document_on_local_failure() -> None:
    session = FakeSession(fail_commit=True)
    provider = FakeProvider()
    service = _service(session, provider, FakeOss(), _knowledge_base())

    with pytest.raises(RuntimeError, match="local commit failed"):
        await service.upload_file(
            principal=Principal(org_id=1, user_id=2),
            knowledge_base_id=83,
            filename="说明.docx",
            content_type="application/octet-stream",
            content=b"doc-content",
        )

    assert session.rolled_back is True
    assert provider.deleted == ["doc-1"]


async def test_weknora_duplicate_upload_does_not_create_local_mapping() -> None:
    session = FakeSession()
    provider = FakeProvider()
    provider.upload_error = PersonalKnowledgeProviderError(
        "upload_document_failed", status_code=409
    )
    service = _service(session, provider, FakeOss(), _knowledge_base())

    with pytest.raises(PersonalKnowledgeError) as raised:
        await service.upload_file(
            principal=Principal(org_id=1, user_id=2),
            knowledge_base_id=83,
            filename="说明.docx",
            content_type="application/octet-stream",
            content=b"doc-content",
        )

    assert raised.value.code == "personal_knowledge_file_duplicate"
    assert raised.value.status_code == 409
    assert session.file is None


async def test_filebase_upload_keeps_oss_contract() -> None:
    session = FakeSession()
    provider = FakeProvider()
    oss = FakeOss()
    service = _service(session, provider, oss, _knowledge_base("filebase"))

    await service.upload_file(
        principal=Principal(org_id=1, user_id=2),
        knowledge_base_id=83,
        filename="说明.docx",
        content_type="application/octet-stream",
        content=b"doc-content",
    )

    assert len(oss.calls) == 1
    assert provider.uploads == []
    assert session.file is not None
    assert session.file.oss_url == "http://oss.example/file"


@pytest.mark.parametrize(
    ("provider_status", "expected_status"),
    [
        ("pending", 0),
        ("processing", 0),
        ("finalizing", 0),
        ("completed", 1),
        ("failed", -1),
        ("cancelled", -1),
    ],
)
async def test_weknora_status_sync_maps_terminal_and_nonterminal_states(
    provider_status: str,
    expected_status: int,
) -> None:
    session = FakeSession()
    provider = FakeProvider()
    provider.document = WeKnoraKnowledgeDocument("doc-1", provider_status)
    service = _service(session, provider, FakeOss(), _knowledge_base())
    file = _file()

    await service._sync_weknora_files([file])

    assert file.file_status == expected_status
    assert file.provider_status == provider_status
    assert file.provider_synced_at is not None
    assert file.provider_error is None
    assert session.commit_count == 1


async def test_weknora_delete_marks_local_only_after_remote_acceptance() -> None:
    session = FakeSession()
    session.file = _file()
    provider = FakeProvider()
    service = _service(session, provider, FakeOss(), _knowledge_base())

    await service.delete_file(
        principal=Principal(org_id=1, user_id=2),
        knowledge_base_id=83,
        file_id=215,
    )

    assert provider.deleted == ["doc-1"]
    assert session.file.is_deleted == 1
    assert session.file.provider_status == "deleting"


async def test_weknora_delete_requires_external_identity() -> None:
    session = FakeSession()
    session.file = _file(provider_document_id=None)
    provider = FakeProvider()
    service = _service(session, provider, FakeOss(), _knowledge_base())

    with pytest.raises(PersonalKnowledgeError) as raised:
        await service.delete_file(
            principal=Principal(org_id=1, user_id=2),
            knowledge_base_id=83,
            file_id=215,
        )

    assert raised.value.code == "personal_knowledge_provider_mapping_required"
    assert raised.value.status_code == 409
    assert provider.deleted == []
    assert session.file.is_deleted == 0


async def test_weknora_delete_normalizes_provider_configuration_failure() -> None:
    session = FakeSession()
    session.file = _file()
    service = _service(session, FakeProvider(), FakeOss(), _knowledge_base())

    def fail_provider(_provider_name: str):
        raise PersonalKnowledgeProviderError("weknora_provider_not_configured")

    service._provider_for = fail_provider  # type: ignore[method-assign]

    with pytest.raises(PersonalKnowledgeError) as raised:
        await service.delete_file(
            principal=Principal(org_id=1, user_id=2),
            knowledge_base_id=83,
            file_id=215,
        )

    assert raised.value.code == "personal_knowledge_delete_failed"
    assert raised.value.status_code == 502
    assert session.file.is_deleted == 0


async def test_weknora_delete_treats_missing_remote_document_as_deleted() -> None:
    session = FakeSession()
    session.file = _file()
    provider = FakeProvider()
    provider.delete_error = PersonalKnowledgeProviderError(
        "delete_document_failed", status_code=404
    )
    service = _service(session, provider, FakeOss(), _knowledge_base())

    await service.delete_file(
        principal=Principal(org_id=1, user_id=2),
        knowledge_base_id=83,
        file_id=215,
    )

    assert session.file.is_deleted == 1


async def test_weknora_delete_rechecks_deleting_state_after_uncertain_response() -> None:
    session = FakeSession()
    session.file = _file()
    provider = FakeProvider()
    provider.delete_error = PersonalKnowledgeProviderError("delete_document_failed")
    provider.document = WeKnoraKnowledgeDocument("doc-1", "deleting")
    service = _service(session, provider, FakeOss(), _knowledge_base())

    await service.delete_file(
        principal=Principal(org_id=1, user_id=2),
        knowledge_base_id=83,
        file_id=215,
    )

    assert session.file.is_deleted == 1


async def test_weknora_file_content_uses_provider_document_stream() -> None:
    session = FakeSession()
    provider = FakeProvider()
    service = _service(session, provider, FakeOss(), _knowledge_base())
    access = PersonalKnowledgeFileAccess(
        knowledge_base=_knowledge_base(),
        file=_file(),
        role="owner",
    )

    async with service.stream_file_content(access=access, download=True) as response:
        assert await response.aread() == b"stream-content"

    assert provider.stream_calls == [("doc-1", True)]
