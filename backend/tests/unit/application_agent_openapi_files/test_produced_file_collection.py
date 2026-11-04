from __future__ import annotations

import pytest
from types import SimpleNamespace

import app.services.application_agent_invocation as invocation_module
from app.hermes_runtime.dashboard_protocol import ManagedRuntimeArtifact
from app.models import (
    ApplicationAgent,
    ApplicationAgentConversation,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentRun,
)
from app.services.application_agent_invocation import (
    SqlAlchemyApplicationAgentInvocationService,
)


class _NoopPreparedKnowledge:
    async def prepare_application_agent(self, **_kwargs: object) -> None:
        return None


@pytest.mark.asyncio
async def test_openapi_run_files_are_collected_after_terminal(db, monkeypatch):
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    run = await db.get(ApplicationAgentRun, 501)
    artifact = ManagedRuntimeArtifact(
        relative_path="workspace/sessions/app-session-1/report.pdf",
        mime_type="application/pdf",
        size_bytes=120,
    )
    record = SimpleNamespace(
        file_id=901,
        display_name="report.pdf",
        mime_type="application/pdf",
        size_bytes=120,
    )

    class FakeFileService:
        def __init__(self, session):
            assert session is db

        async def sync_runtime_artifacts(
            self,
            *,
            agent,
            binding,
            conversation,
            run,
            artifacts,
        ):
            assert (agent, binding, conversation, run) == (
                agent,
                binding,
                conversation,
                run,
            )
            assert artifacts == [artifact]
            return [record]

    class FakeAdapter:
        async def list_session_artifacts(
            self,
            endpoint,
            *,
            runtime_session_id,
        ):
            assert endpoint.api_key == "test-only"
            assert runtime_session_id == "app-session-1"
            return [artifact]

    monkeypatch.setattr(
        invocation_module,
        "ApplicationAgentConversationFileService",
        FakeFileService,
    )
    monkeypatch.setattr(
        invocation_module,
        "HermesDashboardProtocolAdapter",
        FakeAdapter,
    )
    monkeypatch.setattr(
        invocation_module,
        "application_agent_file_download_url",
        lambda *, slug, file_id: f"https://api.example/{slug}/{file_id}",
    )
    service = SqlAlchemyApplicationAgentInvocationService(
        db,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )
    service._endpoint = lambda *, agent, binding: type(
        "Endpoint",
        (),
        {"api_key": "test-only"},
    )()

    result = await service._collect_openapi_produced_files(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=run,
    )

    assert result and result[0].model_dump() == {
        "file_id": 901,
        "display_name": "report.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 120,
        "download_url": "https://api.example/report-agent/901",
    }


@pytest.mark.asyncio
async def test_openapi_file_collection_failure_does_not_fail_response(
    db,
    monkeypatch,
):
    agent = await db.get(ApplicationAgent, 101)
    binding = await db.get(ApplicationAgentRuntimeBinding, 301)
    conversation = await db.get(ApplicationAgentConversation, 401)
    run = await db.get(ApplicationAgentRun, 501)

    class FakeFileService:
        def __init__(self, session):
            del session

        async def sync_runtime_artifacts(self, **_kwargs: object):
            raise RuntimeError("runtime listing failed")

    monkeypatch.setattr(
        invocation_module,
        "ApplicationAgentConversationFileService",
        FakeFileService,
    )
    service = SqlAlchemyApplicationAgentInvocationService(
        db,
        prepared_knowledge_service=_NoopPreparedKnowledge(),
    )

    assert await service._collect_openapi_produced_files(
        agent=agent,
        binding=binding,
        conversation=conversation,
        run=run,
    ) is None
