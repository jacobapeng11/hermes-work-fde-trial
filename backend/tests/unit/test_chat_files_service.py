from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.chat_files import (
    ChatFileError,
    ChatFileIndexService,
    ChatFilePermissionError,
    ChatFileRecord,
    RegisterChatFileCommand,
    SqlAlchemyChatFileStore,
    normalize_workspace_relative_path,
    session_workspace_relative_root,
)
from app.services.runtime_workspace import Principal


class FakeChatFileStore:
    def __init__(self) -> None:
        self.records: dict[int, ChatFileRecord] = {}
        self.next_file_id = 1
        self.get_files_calls: list[list[int]] = []

    async def session_workspace_relative_root(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> str:
        if runtime_workspace_id != 10:
            raise ChatFilePermissionError("runtime workspace not found")
        return f"workspace/sessions/{runtime_session_id}"

    async def register_file(self, *, org_id: int, user_id: int, command: RegisterChatFileCommand):
        if command.runtime_workspace_id != 10:
            raise ChatFilePermissionError("runtime workspace not found")
        record = ChatFileRecord(
            file_id=self.next_file_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_session_id=command.runtime_session_id,
            runtime_run_id=command.runtime_run_id,
            relative_path=command.relative_path,
            display_name=command.relative_path.rsplit("/", 1)[-1],
            mime_type=command.mime_type,
            size_bytes=command.size_bytes,
            source_type=command.source_type,
        )
        self.records[record.file_id] = record
        self.next_file_id += 1
        return record

    async def list_files(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str | None = None,
        runtime_run_id: str | None = None,
    ):
        return [
            record for record in self.records.values()
            if record.runtime_workspace_id == runtime_workspace_id
            and (runtime_session_id is None or record.runtime_session_id == runtime_session_id)
            and (runtime_run_id is None or record.runtime_run_id == runtime_run_id)
        ]

    async def get_file(self, *, org_id: int, user_id: int, file_id: int):
        return self.records[file_id]

    async def get_files(self, *, org_id: int, user_id: int, file_ids: list[int]):
        self.get_files_calls.append(list(file_ids))
        return [record for file_id in file_ids if (record := self.records.get(file_id)) is not None]

    async def rename_file(self, *, org_id: int, user_id: int, file_id: int, relative_path: str):
        record = self.records[file_id]
        renamed = ChatFileRecord(
            file_id=record.file_id,
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_session_id=record.runtime_session_id,
            runtime_run_id=record.runtime_run_id,
            relative_path=relative_path,
            display_name=relative_path.rsplit("/", 1)[-1],
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            source_type=record.source_type,
        )
        self.records[file_id] = renamed
        return renamed

    async def delete_file(self, *, org_id: int, user_id: int, file_id: int):
        return self.records.pop(file_id)


def test_normalize_workspace_relative_path_allows_workspace_files() -> None:
    assert normalize_workspace_relative_path("workspace/uploads/report.txt") == "workspace/uploads/report.txt"


def test_session_workspace_relative_root_uses_expert_profile_path() -> None:
    assert session_workspace_relative_root(
        "rs-10-1",
        runtime_agent_profile_id=69,
        hermes_profile_name="expert-69",
    ) == "workspace/experts/69/sessions/rs-10-1"


async def test_chat_file_index_service_keeps_expert_artifact_under_its_session_root() -> None:
    class ExpertStore(FakeChatFileStore):
        async def session_workspace_relative_root(self, **_kwargs) -> str:
            return "workspace/experts/69/sessions/rs-10-1"

    service = ChatFileIndexService(ExpertStore())
    principal = Principal(org_id=7, user_id=9)

    artifact = await service.register_file(
        principal=principal,
        command=RegisterChatFileCommand(
            runtime_workspace_id=10,
            runtime_session_id="rs-10-1",
            runtime_run_id="rr-10-1",
            relative_path="workspace/experts/69/sessions/rs-10-1/标准电子发票_已打码.pdf",
            mime_type="application/pdf",
            size_bytes=2048,
            source_type="tool",
        ),
    )
    upload = await service.register_file(
        principal=principal,
        command=RegisterChatFileCommand(
            runtime_workspace_id=10,
            runtime_session_id="rs-10-1",
            runtime_run_id=None,
            relative_path="workspace/uploads/input.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            source_type="upload",
        ),
    )

    assert artifact.relative_path == "workspace/experts/69/sessions/rs-10-1/标准电子发票_已打码.pdf"
    assert upload.relative_path == "workspace/experts/69/sessions/rs-10-1/uploads/input.pdf"

    with pytest.raises(ChatFileError, match="current session directory"):
        await service.register_file(
            principal=principal,
            command=RegisterChatFileCommand(
                runtime_workspace_id=10,
                runtime_session_id="rs-10-1",
                runtime_run_id=None,
                relative_path="workspace/experts/70/sessions/rs-10-1/other.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                source_type="tool",
            ),
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "home/uploads/report.txt",
        "workspace/.env",
        "workspace/config.yaml",
        "workspace/logs/runtime.log",
        "workspace/state.sqlite",
        "workspace/state.db",
        "../workspace/report.txt",
        "/workspace/report.txt",
        "workspace\\report.txt",
    ],
)
def test_normalize_workspace_relative_path_rejects_forbidden_paths(relative_path: str) -> None:
    with pytest.raises(ChatFileError):
        normalize_workspace_relative_path(relative_path)


async def test_chat_file_index_service_registers_and_filters_visible_workspace_files() -> None:
    store = FakeChatFileStore()
    service = ChatFileIndexService(store)
    principal = Principal(org_id=7, user_id=9)

    first = await service.register_file(
        principal=principal,
        command=RegisterChatFileCommand(
            runtime_workspace_id=10,
            runtime_session_id="rs-10-1",
            runtime_run_id="rr-10-1",
            relative_path="workspace/uploads/input.txt",
            mime_type="text/plain",
            size_bytes=42,
            source_type="upload",
        ),
    )
    await service.register_file(
        principal=principal,
        command=RegisterChatFileCommand(
            runtime_workspace_id=10,
            runtime_session_id="rs-10-2",
            runtime_run_id=None,
            relative_path="workspace/exports/other.csv",
            mime_type="text/csv",
            size_bytes=10,
            source_type="export",
        ),
    )

    result = await service.list_files(
        principal=principal,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-1",
    )

    assert result == [first]
    assert first.display_name == "input.txt"
    assert first.relative_path == "workspace/sessions/rs-10-1/uploads/input.txt"


async def test_chat_file_index_service_hides_internal_virtualenv_artifacts() -> None:
    store = FakeChatFileStore()
    store.records[1] = ChatFileRecord(
        1,
        10,
        "rs-10-1",
        "rr-10-1",
        "workspace/sessions/rs-10-1/.venv/lib/python3.13/site-packages/openpyxl/__init__.pyc",
        "__init__.pyc",
        "application/octet-stream",
        200,
        "runtime",
    )
    service = ChatFileIndexService(store)

    result = await service.list_files(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=10,
        runtime_session_id="rs-10-1",
    )

    assert result == []


async def test_chat_file_index_service_collapses_and_mutates_legacy_path_duplicates() -> None:
    store = FakeChatFileStore()
    store.records = {
        1: ChatFileRecord(
            1, 10, "rs-10-1", "rr-old", "workspace/sessions/rs-10-1/lesson.pptx",
            "lesson.pptx", "application/pptx", 100, "tool",
        ),
        2: ChatFileRecord(
            2, 10, "rs-10-1", "rr-new", "workspace/sessions/rs-10-1/lesson.pptx",
            "lesson.pptx", "application/pptx", 120, "tool",
        ),
        3: ChatFileRecord(
            3, 10, "rs-10-2", "rr-other", "workspace/sessions/rs-10-2/lesson.pptx",
            "lesson.pptx", "application/pptx", 80, "tool",
        ),
        4: ChatFileRecord(
            4, 10, "rs-10-1", "rr-new", "workspace/sessions/rs-10-1/notes.js",
            "notes.js", "text/javascript", 20, "tool",
        ),
    }
    service = ChatFileIndexService(store)
    principal = Principal(org_id=7, user_id=9)

    listed = await service.list_files(
        principal=principal,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-1",
    )

    assert [record.file_id for record in listed] == [4, 2]

    renamed = await service.rename_file(
        principal=principal,
        file_id=2,
        relative_path="workspace/renamed.pptx",
    )

    assert renamed.file_id == 2
    assert store.records[1].relative_path == "workspace/sessions/rs-10-1/renamed.pptx"
    assert store.records[2].relative_path == "workspace/sessions/rs-10-1/renamed.pptx"
    assert store.records[4].relative_path == "workspace/sessions/rs-10-1/notes.js"

    deleted = await service.delete_file(principal=principal, file_id=2)

    assert deleted.file_id == 2
    assert 1 not in store.records
    assert 2 not in store.records
    assert 3 in store.records
    assert 4 in store.records


async def test_chat_file_index_service_bulk_get_preserves_first_seen_order_and_rejects_missing() -> None:
    store = FakeChatFileStore()
    store.records = {
        1: ChatFileRecord(1, 10, "rs-10-1", None, "workspace/a.txt", "a.txt", "text/plain", 1, "upload"),
        2: ChatFileRecord(2, 10, "rs-10-1", None, "workspace/b.txt", "b.txt", "text/plain", 1, "upload"),
    }
    service = ChatFileIndexService(store)
    principal = Principal(org_id=7, user_id=9)

    result = await service.get_files(principal=principal, file_ids=[2, 1, 2])

    assert [record.file_id for record in result] == [2, 1]
    assert store.get_files_calls == [[2, 1]]

    with pytest.raises(ChatFilePermissionError, match="chat file not found"):
        await service.get_files(principal=principal, file_ids=[1, 3])


async def test_sqlalchemy_chat_file_store_bulk_get_uses_one_query_and_maps_context_ids() -> None:
    artifact = SimpleNamespace(
        id=2,
        runtime_workspace_id=10,
        relative_path="workspace/b.txt",
        mime_type="text/plain",
        size_bytes=1,
        source_type="upload",
        created_at=None,
    )
    result = SimpleNamespace(all=lambda: [(artifact, "rs-10-1", "rr-10-1", "rs-from-run")])
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    store = SqlAlchemyChatFileStore(db)

    records = await store.get_files(org_id=7, user_id=9, file_ids=[2])

    assert records == [
        ChatFileRecord(2, 10, "rs-10-1", "rr-10-1", "workspace/b.txt", "b.txt", "text/plain", 1, "upload")
    ]
    db.execute.assert_awaited_once()


async def test_sqlalchemy_chat_file_store_resolves_expert_session_root() -> None:
    session = SimpleNamespace(runtime_session_id="rs-10-1")
    profile = SimpleNamespace(id=69, hermes_profile_name="expert-69")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (session, profile)))
    )

    root = await SqlAlchemyChatFileStore(db).session_workspace_relative_root(
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-1",
    )

    assert root == "workspace/experts/69/sessions/rs-10-1"


async def test_sqlalchemy_chat_file_store_upserts_latest_session_path_and_retires_duplicates() -> None:
    newest = SimpleNamespace(
        id=12,
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        session_id=3,
        run_id=8,
        relative_path="workspace/sessions/rs-10-1/lesson.pptx",
        mime_type="application/old",
        size_bytes=100,
        source_type="tool",
        visibility="owner",
        status="active",
        created_at=None,
    )
    older = SimpleNamespace(**{**newest.__dict__, "id": 11})
    scalar_result = SimpleNamespace(all=lambda: [newest, older])
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: scalar_result)),
        add=Mock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    store = SqlAlchemyChatFileStore(db)
    store._resolve_context = AsyncMock(return_value=(3, 9))
    expected = ChatFileRecord(
        12, 10, "rs-10-1", "rr-new", newest.relative_path, "lesson.pptx",
        "application/pptx", 200, "tool",
    )
    store._record_for_artifact = AsyncMock(return_value=expected)

    result = await store.register_file(
        org_id=7,
        user_id=9,
        command=RegisterChatFileCommand(
            runtime_workspace_id=10,
            runtime_session_id="rs-10-1",
            runtime_run_id="rr-new",
            relative_path=newest.relative_path,
            mime_type="application/pptx",
            size_bytes=200,
            source_type="tool",
        ),
    )

    assert result == expected
    assert newest.run_id == 9
    assert newest.mime_type == "application/pptx"
    assert newest.size_bytes == 200
    assert newest.source_type == "tool"
    assert newest.visibility == "owner"
    assert newest.status == "active"
    assert older.status == "deleted"
    db.add.assert_not_called()
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(newest)


async def test_chat_file_index_service_blocks_invalid_source_and_size() -> None:
    service = ChatFileIndexService(FakeChatFileStore())
    principal = Principal(org_id=7, user_id=9)

    with pytest.raises(ChatFileError):
        await service.register_file(
            principal=principal,
            command=RegisterChatFileCommand(
                runtime_workspace_id=10,
                runtime_session_id=None,
                runtime_run_id=None,
                relative_path="workspace/uploads/input.txt",
                mime_type=None,
                size_bytes=0,
                source_type="runtime",
            ),
        )

    with pytest.raises(ChatFileError):
        await service.register_file(
            principal=principal,
            command=RegisterChatFileCommand(
                runtime_workspace_id=10,
                runtime_session_id=None,
                runtime_run_id=None,
                relative_path="workspace/uploads/input.txt",
                mime_type=None,
                size_bytes=-1,
                source_type="upload",
            ),
        )
