from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.hermes_runtime.dashboard_protocol import ManagedRuntimeArtifact
from app.models import (
    ApplicationAgent,
    ApplicationAgentConversation,
    ApplicationAgentConversationFileArtifact,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentRun,
)
from app.services.application_agent_conversation_files import (
    ApplicationAgentConversationFileError,
    ApplicationAgentConversationFileService,
)


@pytest.mark.asyncio
async def test_runtime_artifacts_are_idempotent_and_delete_missing_files(
    db,
) -> None:
    service = ApplicationAgentConversationFileService(db)
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    run = await db.get(ApplicationAgentRun, 501)

    first = await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=run,
        artifacts=[
            ManagedRuntimeArtifact(
                relative_path=(
                    "workspace/sessions/app-session-1/report.pdf"
                ),
                mime_type="application/pdf",
                size_bytes=120,
            ),
            ManagedRuntimeArtifact(
                relative_path=(
                    "workspace/sessions/app-session-1/data.xlsx"
                ),
                mime_type=(
                    "application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"
                ),
                size_bytes=240,
            ),
        ],
    )

    assert [item.display_name for item in first] == [
        "data.xlsx",
        "report.pdf",
    ]
    assert (
        await db.execute(
            select(func.count()).select_from(
                ApplicationAgentConversationFileArtifact
            )
        )
    ).scalar_one() == 2

    second = await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=run,
        artifacts=[
            ManagedRuntimeArtifact(
                relative_path=(
                    "workspace/sessions/app-session-1/report.pdf"
                ),
                mime_type="application/pdf",
                size_bytes=180,
            ),
        ],
    )
    rows = (
        (
            await db.execute(
                select(ApplicationAgentConversationFileArtifact).order_by(
                    ApplicationAgentConversationFileArtifact.relative_path
                )
            )
        )
        .scalars()
        .all()
    )

    assert len(second) == 1
    assert second[0].size_bytes == 180
    assert len(rows) == 2
    assert [(row.relative_path, row.status, row.size_bytes) for row in rows] == [
        (
            "workspace/sessions/app-session-1/data.xlsx",
            "deleted",
            240,
        ),
        (
            "workspace/sessions/app-session-1/report.pdf",
            "active",
            180,
        ),
    ]


@pytest.mark.asyncio
async def test_runtime_artifact_cannot_escape_conversation_session(db) -> None:
    service = ApplicationAgentConversationFileService(db)
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    run = await db.get(ApplicationAgentRun, 501)

    with pytest.raises(ApplicationAgentConversationFileError):
        await service.sync_runtime_artifacts(
            agent=agent,
            binding=binding,
            conversation=conversation,
            run=run,
            artifacts=[
                ManagedRuntimeArtifact(
                    relative_path="workspace/sessions/other/report.pdf",
                    mime_type="application/pdf",
                    size_bytes=1,
                )
            ],
        )


@pytest.mark.asyncio
async def test_unchanged_old_file_is_not_reattributed_to_later_run(db) -> None:
    service = ApplicationAgentConversationFileService(db)
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    first_run = await db.get(ApplicationAgentRun, 501)
    artifact = ManagedRuntimeArtifact(
        relative_path="workspace/sessions/app-session-1/report.pdf",
        mime_type="application/pdf",
        size_bytes=120,
    )
    await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=first_run,
        artifacts=[artifact],
    )
    second_run = ApplicationAgentRun(
        id=503,
        org_id=1,
        application_agent_id=101,
        runtime_binding_id=301,
        application_client_id=201,
        conversation_id=401,
        runtime_session_id="app-session-1",
        runtime_run_id="run-3",
        subject_type="external_user",
        subject_service_id="ext-1",
        channel="openapi",
        status="running",
        request_id="request-3",
    )
    db.add(second_run)
    await db.flush()

    produced = await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=second_run,
        artifacts=[artifact],
    )
    row = await db.scalar(
        select(ApplicationAgentConversationFileArtifact).where(
            ApplicationAgentConversationFileArtifact.conversation_id == 401
        )
    )
    run_filter = await service.list_files(
        slug="report-agent",
        client_id="client-201",
        client_secret="client-secret",
        delegated_principal=None,
        conversation_key="default",
        external_user_id="ext-1",
        runtime_run_id="run-3",
    )

    assert produced == []
    assert row is not None
    assert row.application_agent_run_id == 501
    assert run_filter == []


@pytest.mark.asyncio
async def test_same_metadata_overwrite_uses_runtime_mtime(db) -> None:
    service = ApplicationAgentConversationFileService(db)
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    first_run = await db.get(ApplicationAgentRun, 501)
    artifact = ManagedRuntimeArtifact(
        relative_path="workspace/sessions/app-session-1/report.pdf",
        mime_type="application/pdf",
        size_bytes=120,
        mtime=1_000.0,
    )
    await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=first_run,
        artifacts=[artifact],
    )
    second_run = ApplicationAgentRun(
        id=503,
        org_id=1,
        application_agent_id=101,
        runtime_binding_id=301,
        application_client_id=201,
        conversation_id=401,
        runtime_session_id="app-session-1",
        runtime_run_id="run-3",
        subject_type="external_user",
        subject_service_id="ext-1",
        channel="openapi",
        status="running",
        request_id="request-3",
        started_at=datetime.fromtimestamp(2_000, timezone.utc).replace(
            tzinfo=None
        ),
    )
    db.add(second_run)
    await db.flush()
    overwritten = replace(artifact, mtime=2_010.0)

    produced = await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=second_run,
        artifacts=[overwritten],
    )
    row = await db.scalar(
        select(ApplicationAgentConversationFileArtifact).where(
            ApplicationAgentConversationFileArtifact.conversation_id == 401
        )
    )

    assert len(produced) == 1
    assert row is not None
    assert row.application_agent_run_id == 503


@pytest.mark.asyncio
async def test_download_context_rejects_cross_agent_slug(db) -> None:
    service = ApplicationAgentConversationFileService(db)
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    run = await db.get(ApplicationAgentRun, 501)
    records = await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=run,
        artifacts=[
            ManagedRuntimeArtifact(
                relative_path="workspace/sessions/app-session-1/report.pdf",
                mime_type="application/pdf",
                size_bytes=120,
            )
        ],
    )

    download = await service.file_for_download(
        slug="report-agent",
        file_id=records[0].file_id,
    )
    assert download.record.file_id == records[0].file_id

    with pytest.raises(ApplicationAgentConversationFileError):
        await service.file_for_download(
            slug="other-agent",
            file_id=records[0].file_id,
        )


@pytest.mark.asyncio
async def test_file_listing_is_isolated_by_client_subject_and_conversation(
    db,
) -> None:
    service = ApplicationAgentConversationFileService(db)
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    other_conversation = await db.get(ApplicationAgentConversation, 402)
    run = await db.get(ApplicationAgentRun, 501)
    other_run = await db.get(ApplicationAgentRun, 502)
    await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=run,
        artifacts=[
            ManagedRuntimeArtifact(
                relative_path="workspace/sessions/app-session-1/report.pdf",
                mime_type="application/pdf",
                size_bytes=120,
            )
        ],
    )
    await service.sync_runtime_artifacts(
        agent=agent,
        binding=binding,
        conversation=other_conversation,
        run=other_run,
        artifacts=[
            ManagedRuntimeArtifact(
                relative_path="workspace/sessions/app-session-2/other.pdf",
                mime_type="application/pdf",
                size_bytes=99,
            )
        ],
    )

    records = await service.list_files(
        slug="report-agent",
        client_id="client-201",
        client_secret="client-secret",
        delegated_principal=None,
        conversation_key="default",
        external_user_id="ext-1",
        runtime_run_id="run-1",
    )

    assert len(records) == 1
    assert records[0].relative_path == (
        "workspace/sessions/app-session-1/report.pdf"
    )

    other_records = await service.list_files(
        slug="report-agent",
        client_id="client-201",
        client_secret="client-secret",
        delegated_principal=None,
        conversation_key="default",
        external_user_id="ext-2",
    )
    assert [record.relative_path for record in other_records] == [
        "workspace/sessions/app-session-2/other.pdf"
    ]
    with pytest.raises(ApplicationAgentConversationFileError):
        await service.list_files(
            slug="report-agent",
            client_id="client-202",
            client_secret="second-client-secret",
            delegated_principal=None,
            conversation_key="default",
            external_user_id="ext-1",
        )
