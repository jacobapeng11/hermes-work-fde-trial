from __future__ import annotations

import base64

import httpx
import pytest
from pydantic import ValidationError

from app.api.openapi import application_agents as openapi_application_agents
from app.config import settings
from app.database import get_db
from app.hermes_runtime.dashboard_protocol import ManagedRuntimeArtifact
from app.hermes_runtime.protocol import HermesProtocolError
from app.main import app
from app.models import (
    ApplicationAgent,
    ApplicationAgentConversation,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentRun,
)
from app.schemas.application_agent_invocation import (
    ApplicationAgentResponseRequest,
)
from app.services.application_agent_conversation_files import (
    ApplicationAgentConversationFileService,
)
from app.services.file_url_signer import sign_file_download_url


def _basic(client_id: str, secret: str) -> str:
    encoded = base64.b64encode(
        f"{client_id}:{secret}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {encoded}"


async def _seed_conversation_file(db, *, size_bytes: int = 120):
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
                size_bytes=size_bytes,
            )
        ],
    )
    return records[0]


@pytest.mark.asyncio
async def test_conversation_file_list_returns_signed_metadata(
    db,
    monkeypatch,
) -> None:
    record = await _seed_conversation_file(db)

    async def database():
        yield db

    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api.example")
    monkeypatch.setattr(
        settings,
        "FILE_DOWNLOAD_SIGNING_SECRET",
        "unit-test-openapi-file-signing-secret",
    )
    monkeypatch.setattr(settings, "FILE_DOWNLOAD_URL_TTL_SECONDS", 600)
    app.dependency_overrides[get_db] = database
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/openapi/v1/application-agents/report-agent"
                "/conversations/default/files",
                headers={
                    "Authorization": _basic("client-201", "client-secret")
                },
                params={
                    "external_user_id": "ext-1",
                    "runtime_run_id": "run-1",
                },
            )
            cross_subject = await client.get(
                "/openapi/v1/application-agents/report-agent"
                "/conversations/default/files",
                headers={
                    "Authorization": _basic("client-201", "client-secret")
                },
                params={"external_user_id": "ext-2"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0] == {
            "file_id": record.file_id,
            "display_name": "report.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 120,
            "source_type": "runtime",
            "runtime_run_id": "run-1",
            "created_at": payload[0]["created_at"],
            "download_url": payload[0]["download_url"],
        }
        assert payload[0]["download_url"].startswith(
            "https://api.example/openapi/v1/application-agents/report-agent"
            f"/files/{record.file_id}/content?"
        )
        assert cross_subject.status_code == 200
        assert cross_subject.json() == []
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_signed_download_proxies_runtime_file_and_rejects_tampering(
    db,
    monkeypatch,
) -> None:
    record = await _seed_conversation_file(db, size_bytes=4)

    class FakeAdapter:
        async def stream_managed_artifact(
            self,
            endpoint,
            *,
            relative_path,
        ):
            assert relative_path == record.relative_path
            yield b"PDF"

    monkeypatch.setattr(
        openapi_application_agents,
        "HermesDashboardProtocolAdapter",
        FakeAdapter,
    )
    monkeypatch.setattr(
        settings,
        "FILE_DOWNLOAD_SIGNING_SECRET",
        "unit-test-openapi-file-signing-secret",
    )

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            invalid = await client.get(
                "/openapi/v1/application-agents/report-agent"
                f"/files/{record.file_id}/content",
                params={"expires": 1, "signature": "0" * 64},
            )
            cross_agent_url = sign_file_download_url(
                base_url="http://test",
                slug="other-agent",
                file_id=record.file_id,
                secret="unit-test-openapi-file-signing-secret",
                ttl_seconds=600,
            )
            cross_agent = await client.get(cross_agent_url)
            valid_url = sign_file_download_url(
                base_url="http://test",
                slug="report-agent",
                file_id=record.file_id,
                secret="unit-test-openapi-file-signing-secret",
                ttl_seconds=600,
            )
            valid = await client.get(valid_url)

        assert invalid.status_code == 403
        assert invalid.json() == {"detail": "file_download_url_invalid"}
        assert cross_agent.status_code == 404
        assert cross_agent.json() == {"detail": "application_file_not_found"}
        assert valid.status_code == 200
        assert valid.content == b"PDF"
        assert valid.headers["content-type"] == "application/pdf"
        assert valid.headers["content-length"] == "4"
        assert valid.headers["cache-control"] == "no-store"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_runtime_download_failure_maps_to_stable_error(
    db,
    monkeypatch,
) -> None:
    record = await _seed_conversation_file(db, size_bytes=4)

    class FailingAdapter:
        async def stream_managed_artifact(
            self,
            endpoint,
            *,
            relative_path,
        ):
            assert relative_path == record.relative_path
            raise HermesProtocolError("Runtime Supervisor is unavailable")
            yield b""  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(
        openapi_application_agents,
        "HermesDashboardProtocolAdapter",
        FailingAdapter,
    )
    monkeypatch.setattr(
        settings,
        "FILE_DOWNLOAD_SIGNING_SECRET",
        "unit-test-openapi-file-signing-secret",
    )

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get(
                sign_file_download_url(
                    base_url="http://test",
                    slug="report-agent",
                    file_id=record.file_id,
                    secret="unit-test-openapi-file-signing-secret",
                    ttl_seconds=600,
                )
            )

        assert response.status_code == 502
        assert response.json() == {
            "detail": "application_agent_runtime_failed"
        }
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_first_stage_contract_has_no_upload_attachment_route_or_field() -> None:
    paths = {route.path for route in openapi_application_agents.router.routes}

    assert "/application-agents/{slug}/conversations/{conversation_key}/files" in paths
    assert "/application-agents/{slug}/files/{file_id}/content" in paths
    assert not any(path.endswith("/upload-intent") for path in paths)
    with pytest.raises(ValidationError):
        ApplicationAgentResponseRequest(
            input="hello",
            attachment_file_ids=[1],
        )
