from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentStudioSession,
    Base,
)
from app.models.application_agent import ApplicationAgentStudioFileArtifact
from app.services.application_agent_studio_chat_files import (
    ApplicationAgentStudioChatFileService,
)
from app.services.chat_files import ChatFilePermissionError
from app.services.runtime_edge_grants import RuntimeEdgeGrantService
from app.services.runtime_workspace import Principal


ORG_ID = 1
USER_ID = 990095
AGENT_ID = 55
SESSION_ID = "studio-55-990095-RmgdLsLHkjD8AhjO"
PRINCIPAL = Principal(org_id=ORG_ID, user_id=USER_ID)
BINDING = SimpleNamespace(org_id=ORG_ID, application_agent_id=AGENT_ID)


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger,
    _compiler: object,
    **_kwargs: object,
) -> str:
    return "INTEGER"


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                ApplicationAgent.__table__,
                ApplicationAgentStudioSession.__table__,
                ApplicationAgentRun.__table__,
                ApplicationAgentStudioFileArtifact.__table__,
            ],
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    try:
        session.add(
            ApplicationAgent(
                id=AGENT_ID,
                org_id=ORG_ID,
                slug="crc-1",
                name="crc-1",
                status="published",
                created_by_user_id=USER_ID,
                updated_by_user_id=USER_ID,
            )
        )
        session.add(
            ApplicationAgentStudioSession(
                id=193,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                user_id=USER_ID,
                runtime_session_id=SESSION_ID,
                status="active",
            )
        )
        session.add(
            ApplicationAgentStudioSession(
                id=194,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                user_id=USER_ID,
                runtime_session_id="studio-55-990095-other",
                status="active",
            )
        )
        await session.commit()
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_upload_intent_reserves_pending_and_runtime_confirmation_activates(
    db: AsyncSession,
) -> None:
    service = ApplicationAgentStudioChatFileService(db)

    pending = await service.reserve_upload(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="report.pdf",
        mime_type="application/pdf",
        size_bytes=17,
    )

    assert pending.status == "pending"
    assert pending.runtime_workspace_id == AGENT_ID
    assert pending.runtime_session_id == SESSION_ID
    assert pending.relative_path == (
        f"workspace/sessions/{SESSION_ID}/{pending.file_id}/report.pdf"
    )
    assert (
        await service.list_files(
            principal=PRINCIPAL,
            application_agent_id=AGENT_ID,
            runtime_session_id=SESSION_ID,
        )
        == []
    )

    await service.apply_file_status(
        binding=BINDING,
        projection_type="file.ready",
        payload={
            "file_id": pending.file_id,
            "org_id": ORG_ID,
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "relative_path": pending.relative_path,
            "display_name": "report.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 17,
            "sha256": "a" * 64,
        },
    )

    active = await service.get_file(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        file_id=pending.file_id,
    )
    assert active.status == "active"
    assert active.size_bytes == 17
    assert active.sha256_digest == "a" * 64
    listed = await service.list_files(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
    )
    assert [record.file_id for record in listed] == [pending.file_id]


@pytest.mark.asyncio
async def test_same_name_uploads_receive_distinct_paths_and_pending_expires(
    db: AsyncSession,
) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    first = await service.reserve_upload(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="report.pdf",
        size_bytes=1,
    )
    artifact = await db.get(ApplicationAgentStudioFileArtifact, first.file_id)
    assert artifact is not None
    artifact.created_at = datetime.utcnow() - timedelta(minutes=11)
    await db.flush()

    second = await service.reserve_upload(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="report.pdf",
        size_bytes=2,
    )

    assert first.relative_path != second.relative_path
    assert artifact.status == "failed"
    expired = await service.get_file(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        file_id=first.file_id,
        include_pending=True,
    )
    assert expired.status == "failed"


@pytest.mark.asyncio
async def test_runtime_file_failure_marks_pending_failed(db: AsyncSession) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    record = await service.reserve_upload(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="failed.txt",
        size_bytes=1,
    )

    await service.apply_file_status(
        binding=BINDING,
        projection_type="file.failed",
        payload={
            "file_id": record.file_id,
            "org_id": ORG_ID,
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "relative_path": record.relative_path,
        },
    )

    failed = await service.get_file(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        file_id=record.file_id,
        include_pending=True,
    )
    assert failed.status == "failed"
    assert (
        await service.list_files(
            principal=PRINCIPAL,
            application_agent_id=AGENT_ID,
            runtime_session_id=SESSION_ID,
        )
        == []
    )


@pytest.mark.asyncio
async def test_pending_and_duplicate_files_cannot_be_authorized_as_attachments(
    db: AsyncSession,
) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    pending = await service.reserve_upload(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="pending.txt",
        size_bytes=1,
    )

    with pytest.raises(ChatFilePermissionError):
        await service.resolve_attachments(
            principal=PRINCIPAL,
            application_agent_id=AGENT_ID,
            runtime_session_id=SESSION_ID,
            file_ids=[pending.file_id],
        )
    with pytest.raises(ChatFilePermissionError):
        await service.resolve_attachments(
            principal=PRINCIPAL,
            application_agent_id=AGENT_ID,
            runtime_session_id=SESSION_ID,
            file_ids=[pending.file_id, pending.file_id],
        )


@pytest.mark.asyncio
async def test_active_attachments_are_session_scoped_and_ordered(
    db: AsyncSession,
) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    records = []
    for name in ("b.txt", "a.txt"):
        record = await service.reserve_upload(
            principal=PRINCIPAL,
            application_agent_id=AGENT_ID,
            runtime_session_id=SESSION_ID,
            display_name=name,
            size_bytes=1,
        )
        await service.apply_file_status(
            binding=BINDING,
            projection_type="file.ready",
            payload={
                "file_id": record.file_id,
                "org_id": ORG_ID,
                "user_id": USER_ID,
                "session_id": SESSION_ID,
                "relative_path": record.relative_path,
                "size_bytes": 1,
                "sha256": "b" * 64,
            },
        )
        records.append(
            await service.get_file(
                principal=PRINCIPAL,
                application_agent_id=AGENT_ID,
                file_id=record.file_id,
            )
        )

    attachments = await service.resolve_attachments(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        file_ids=[records[1].file_id, records[0].file_id],
    )

    assert attachments == (
        {
            "file_id": str(records[1].file_id),
            "display_name": "a.txt",
            "relative_path": records[1].relative_path,
        },
        {
            "file_id": str(records[0].file_id),
            "display_name": "b.txt",
            "relative_path": records[0].relative_path,
        },
    )


@pytest.mark.asyncio
async def test_runtime_edge_uses_studio_attachment_resolver(db: AsyncSession) -> None:
    file_service = ApplicationAgentStudioChatFileService(db)
    first = await _active_file(file_service, "first.txt")
    second = await _active_file(file_service, "second.txt")

    attachments = await RuntimeEdgeGrantService(
        db  # type: ignore[arg-type]
    )._application_attachments(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        file_ids=[second.file_id, first.file_id],
    )

    assert [item["file_id"] for item in attachments] == [
        str(second.file_id),
        str(first.file_id),
    ]


@pytest.mark.asyncio
async def test_attachment_from_another_studio_session_is_rejected(
    db: AsyncSession,
) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    record = await _active_file(service, "session-scoped.txt")

    with pytest.raises(ChatFilePermissionError):
        await service.resolve_attachments(
            principal=PRINCIPAL,
            application_agent_id=AGENT_ID,
            runtime_session_id="studio-55-990095-other",
            file_ids=[record.file_id],
        )


@pytest.mark.asyncio
async def test_delete_only_soft_deletes_studio_file(db: AsyncSession) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    record = await _active_file(service, "delete.txt")

    deleted = await service.delete_file(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        file_id=record.file_id,
    )
    artifact = await db.get(ApplicationAgentStudioFileArtifact, record.file_id)

    assert deleted.status == "deleted"
    assert artifact is not None
    assert artifact.status == "deleted"
    assert (
        await service.list_files(
            principal=PRINCIPAL,
            application_agent_id=AGENT_ID,
            runtime_session_id=SESSION_ID,
        )
        == []
    )


@pytest.mark.asyncio
async def test_runtime_artifact_renames_within_its_original_directory(
    db: AsyncSession,
) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    db.add(
        ApplicationAgentStudioFileArtifact(
            studio_session_id=193,
            application_agent_run_id=None,
            relative_path=f"workspace/sessions/{SESSION_ID}/outputs/report.pdf",
            mime_type="application/pdf",
            size_bytes=120,
            source_type="runtime",
            status="active",
        )
    )
    await db.commit()
    artifact_id = (
        await db.scalar(
            select(ApplicationAgentStudioFileArtifact).where(
                ApplicationAgentStudioFileArtifact.relative_path
                == f"workspace/sessions/{SESSION_ID}/outputs/report.pdf"
            )
        )
    ).id

    renamed = await service.rename_file(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        file_id=int(artifact_id),
        relative_path=f"workspace/sessions/{SESSION_ID}/outputs/renamed-report.pdf",
    )

    assert renamed.relative_path == (
        f"workspace/sessions/{SESSION_ID}/outputs/renamed-report.pdf"
    )
    assert renamed.display_name == "renamed-report.pdf"


@pytest.mark.asyncio
async def test_file_id_is_not_trusted_across_agents_or_users(
    db: AsyncSession,
) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    record = await _active_file(service, "isolated.txt")

    with pytest.raises(ChatFilePermissionError):
        await service.get_file(
            principal=Principal(org_id=ORG_ID, user_id=USER_ID),
            application_agent_id=56,
            file_id=record.file_id,
        )
    with pytest.raises(ChatFilePermissionError):
        await service.get_file(
            principal=Principal(org_id=ORG_ID, user_id=USER_ID + 1),
            application_agent_id=AGENT_ID,
            file_id=record.file_id,
        )


async def _active_file(service: ApplicationAgentStudioChatFileService, name: str):
    record = await service.reserve_upload(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name=name,
        size_bytes=1,
    )
    await service.apply_file_status(
        binding=BINDING,
        projection_type="file.ready",
        payload={
            "file_id": record.file_id,
            "org_id": ORG_ID,
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "relative_path": record.relative_path,
            "size_bytes": 1,
            "sha256": "c" * 64,
        },
    )
    return await service.get_file(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        file_id=record.file_id,
    )
