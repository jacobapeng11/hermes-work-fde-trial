from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import ApplicationAgent, ApplicationAgentStudioSession, Base
from app.models.application_agent import (
    ApplicationAgentRuntimeBinding,
    ApplicationAgentRun,
    ApplicationAgentStudioFileArtifact,
)
from app.schemas.runtime_internal import (
    RuntimeProjectionItem,
    canonical_payload_digest,
)
from app.services.application_agent_studio_chat_files import (
    ApplicationAgentStudioChatFileService,
)
from app.services.runtime_projection import (
    RuntimeProjectionArtifactError,
    RuntimeProjectionFileError,
    RuntimeProjectionService,
)
from app.services.runtime_workspace import Principal


ORG_ID = 1
USER_ID = 990095
AGENT_ID = 55
SESSION_ID = "studio-55-990095-RmgdLsLHkjD8AhjO"
RUNTIME_RUN_ID = "run-55-001"


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
                ApplicationAgentRuntimeBinding.__table__,
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
            ApplicationAgentRuntimeBinding(
                id=47,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                runtime_id="application-agent-runtime-55",
                k8s_namespace="runtimes",
                knative_service="application-agent-runtime-55",
                runtime_root_pvc="runtime-55",
                runtime_root_host_path="/data/hermes/runtimes/1/55",
                config_generation=11,
                image="hermes-runtime:test",
                api_key_encrypted="runtime-secret",
                status="ready",
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
            ApplicationAgentRun(
                id=701,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                runtime_binding_id=47,
                runtime_session_id=SESSION_ID,
                active_session_key=SESSION_ID,
                runtime_run_id=RUNTIME_RUN_ID,
                subject_type="user",
                subject_user_id=USER_ID,
                channel="studio",
                status="running",
                request_id="request-1",
            )
        )
        await session.commit()
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_file_ready_is_projected_without_run_id(db: AsyncSession) -> None:
    service = ApplicationAgentStudioChatFileService(db)
    pending = await service.reserve_upload(
        principal=Principal(org_id=ORG_ID, user_id=USER_ID),
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="input.txt",
        size_bytes=3,
    )
    payload = {
        "file_id": pending.file_id,
        "org_id": ORG_ID,
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "relative_path": pending.relative_path,
        "size_bytes": 3,
        "sha256": "d" * 64,
    }

    await RuntimeProjectionService(db)._materialize_application(
        _item("file.ready", payload, run_id=None),
        await db.get(ApplicationAgentRuntimeBinding, 47),
    )

    confirmed = await service.get_file(
        principal=Principal(org_id=ORG_ID, user_id=USER_ID),
        application_agent_id=AGENT_ID,
        file_id=pending.file_id,
    )
    assert confirmed.status == "active"


@pytest.mark.asyncio
async def test_application_file_ready_rejects_mismatched_path(db: AsyncSession) -> None:
    file_service = ApplicationAgentStudioChatFileService(db)
    pending = await file_service.reserve_upload(
        principal=Principal(org_id=ORG_ID, user_id=USER_ID),
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="input.txt",
        size_bytes=3,
    )
    payload = {
        "file_id": pending.file_id,
        "org_id": ORG_ID,
        "user_id": USER_ID,
        "session_id": SESSION_ID,
        "relative_path": f"workspace/sessions/{SESSION_ID}/other/input.txt",
        "size_bytes": 3,
        "sha256": "f" * 64,
    }
    binding = await db.get(ApplicationAgentRuntimeBinding, 47)
    assert binding is not None

    with pytest.raises(RuntimeProjectionFileError):
        await RuntimeProjectionService(db)._materialize_application(
            _item("file.ready", payload, run_id=None),
            binding,
        )


@pytest.mark.asyncio
async def test_application_run_artifacts_are_upserted_and_deleted(db: AsyncSession) -> None:
    service = RuntimeProjectionService(db)
    binding = await db.get(ApplicationAgentRuntimeBinding, 47)
    assert binding is not None
    path = f"workspace/sessions/{SESSION_ID}/outputs/report.pdf"
    created_payload = {
        "run_id": RUNTIME_RUN_ID,
        "seq": 1,
        "event_type": "chat.artifact.created",
        "event_data": {
            "relative_path": path,
            "mime_type": "application/pdf",
            "size_bytes": 120,
        },
    }

    await service._materialize_application(
        _item("run.event", created_payload, run_id=RUNTIME_RUN_ID),
        binding,
    )
    artifact = await db.scalar(
        select(ApplicationAgentStudioFileArtifact).where(
            ApplicationAgentStudioFileArtifact.studio_session_id == 193,
            ApplicationAgentStudioFileArtifact.relative_path == path,
        )
    )
    assert artifact is not None
    assert artifact.application_agent_run_id == 701
    assert artifact.status == "active"
    assert artifact.source_type == "runtime"

    updated_payload = {
        "run_id": RUNTIME_RUN_ID,
        "seq": 2,
        "event_type": "chat.artifact.updated",
        "event_data": {
            "relative_path": path,
            "mime_type": "application/pdf",
            "size_bytes": 180,
        },
    }
    await service._materialize_application(
        _item("run.event", updated_payload, run_id=RUNTIME_RUN_ID),
        binding,
    )
    assert artifact.size_bytes == 180

    deleted_payload = {
        "run_id": RUNTIME_RUN_ID,
        "seq": 3,
        "event_type": "chat.artifact.deleted",
        "event_data": {"relative_path": path},
    }
    await service._materialize_application(
        _item("run.event", deleted_payload, run_id=RUNTIME_RUN_ID),
        binding,
    )
    assert artifact.status == "deleted"


@pytest.mark.asyncio
async def test_application_run_artifact_cannot_escape_session_root(
    db: AsyncSession,
) -> None:
    service = RuntimeProjectionService(db)
    binding = await db.get(ApplicationAgentRuntimeBinding, 47)
    assert binding is not None
    payload = {
        "run_id": RUNTIME_RUN_ID,
        "seq": 4,
        "event_type": "chat.artifact.created",
        "event_data": {
            "relative_path": "workspace/sessions/other-session/report.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1,
        },
    }

    with pytest.raises(RuntimeProjectionArtifactError):
        await service._materialize_application(
            _item("run.event", payload, run_id=RUNTIME_RUN_ID),
            binding,
        )


def _item(
    projection_type: str,
    payload: dict,
    *,
    run_id: str | None,
) -> RuntimeProjectionItem:
    return RuntimeProjectionItem(
        projection_id=f"{projection_type}:{payload.get('seq', payload.get('file_id'))}",
        runtime_id="application-agent-runtime-55",
        projection_type=projection_type,
        occurred_at=datetime.now(timezone.utc),
        payload_digest=canonical_payload_digest(payload),
        run_id=run_id,
        seq=payload.get("seq") if run_id is not None else None,
        payload=payload,
    )
