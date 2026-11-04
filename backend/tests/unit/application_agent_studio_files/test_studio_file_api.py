from __future__ import annotations

from types import SimpleNamespace

import pytest
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles

from app.api.v1.user.runtime_workspace import get_principal
from app.api.v1.user.chat_files import get_chat_file_protocol_adapter
from app.config import settings
from app.database import get_db
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.main import app
from app.models import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentStudioFileArtifact,
    ApplicationAgentStudioSession,
    Base,
)
from app.models.application_agent import ApplicationAgentRuntimeBinding
from app.services.application_agent_studio_chat_files import (
    ApplicationAgentStudioChatFileService,
)
from app.services.runtime_ticket_issuer import RuntimeTicketIssuer
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec


ORG_ID = 1
USER_ID = 990095
AGENT_ID = 55
SESSION_ID = "studio-55-990095-RmgdLsLHkjD8AhjO"
RUNTIME_ID = "application-agent-runtime-55"
TICKET_SECRET = "application-file-ticket-secret-000000"
PRINCIPAL = Principal(org_id=ORG_ID, user_id=USER_ID)


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
                ApplicationAgentRuntimeBinding.__table__,
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
            ApplicationAgentRuntimeBinding(
                id=47,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                runtime_id=RUNTIME_ID,
                k8s_namespace="runtimes",
                knative_service=RUNTIME_ID,
                runtime_root_pvc="runtime-55",
                runtime_root_host_path="/data/hermes/runtimes/1/55",
                config_generation=11,
                image="hermes-runtime:test",
                endpoint_base_url="http://runtime.invalid",
                endpoint_host_header="runtime.invalid",
                api_key_encrypted=RuntimeApiKeyCodec().encrypt(TICKET_SECRET),
                status="ready",
            )
        )
        await session.commit()
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_application_studio_upload_download_and_delete_contract(
    db: AsyncSession,
) -> None:
    async def principal() -> Principal:
        return PRINCIPAL

    async def database() -> AsyncSession:
        yield db

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = database
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            intent_response = await client.post(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files/upload-intent",
                json={
                    "runtime_session_id": SESSION_ID,
                    "display_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 17,
                },
            )
            assert intent_response.status_code == 200
            intent = intent_response.json()
            assert intent["runtime_path"] == f"/runtime-files/{RUNTIME_ID}/upload"
            assert intent["file"]["runtime_workspace_id"] == AGENT_ID
            assert intent["file"]["runtime_session_id"] == SESSION_ID
            assert intent["file"]["relative_path"] == (
                f"workspace/sessions/{SESSION_ID}/{intent['file']['file_id']}/report.pdf"
            )

            claims = RuntimeTicketIssuer(
                runtime_id=RUNTIME_ID,
                secret=TICKET_SECRET,
            ).verify_file_transfer(
                intent["file_transfer_ticket"],
                operation="upload",
            )
            assert claims.file_id == intent["file"]["file_id"]
            assert claims.session_id == SESSION_ID
            assert claims.relative_path == intent["file"]["relative_path"]

            pending_response = await client.get(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files/"
                f"{intent['file']['file_id']}/download"
            )
            assert pending_response.status_code == 409
            assert pending_response.json() == {"detail": "chat file is pending"}

            list_response = await client.get(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files",
                params={"runtime_session_id": SESSION_ID},
            )
            assert list_response.status_code == 200
            assert list_response.json() == []

            file_id = intent["file"]["file_id"]
            service = ApplicationAgentStudioChatFileService(db)
            await service.apply_file_status(
                binding=SimpleNamespace(org_id=ORG_ID, application_agent_id=AGENT_ID),
                projection_type="file.ready",
                payload={
                    "file_id": file_id,
                    "org_id": ORG_ID,
                    "user_id": USER_ID,
                    "session_id": SESSION_ID,
                    "relative_path": intent["file"]["relative_path"],
                    "size_bytes": 17,
                    "sha256": "a" * 64,
                },
            )
            await db.commit()

            metadata_response = await client.get(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files/{file_id}/download"
            )
            assert metadata_response.status_code == 200
            assert metadata_response.json()["size_bytes"] == 17

            download_response = await client.post(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files/{file_id}/download-intent"
            )
            assert download_response.status_code == 200
            download_claims = RuntimeTicketIssuer(
                runtime_id=RUNTIME_ID,
                secret=TICKET_SECRET,
            ).verify_file_transfer(
                download_response.json()["file_transfer_ticket"],
                operation="download",
            )
            assert download_claims.file_id == file_id

            delete_response = await client.delete(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files/{file_id}"
            )
            assert delete_response.status_code == 200
            final_list_response = await client.get(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files",
                params={"runtime_session_id": SESSION_ID},
            )
            assert final_list_response.json() == []
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_application_studio_rename_never_sends_unscoped_path_to_runtime(
    db: AsyncSession,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    file_service = ApplicationAgentStudioChatFileService(db)
    record = await file_service.reserve_upload(
        principal=PRINCIPAL,
        application_agent_id=AGENT_ID,
        runtime_session_id=SESSION_ID,
        display_name="original.txt",
        size_bytes=1,
    )
    await file_service.apply_file_status(
        binding=SimpleNamespace(org_id=ORG_ID, application_agent_id=AGENT_ID),
        projection_type="file.ready",
        payload={
            "file_id": record.file_id,
            "org_id": ORG_ID,
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "relative_path": record.relative_path,
            "size_bytes": 1,
            "sha256": "e" * 64,
        },
    )
    await db.commit()

    class RecordingAdapter:
        def __init__(self) -> None:
            self.targets: list[str] = []

        async def rename_managed_artifact(
            self,
            _endpoint: RuntimeEndpoint,
            *,
            source_relative_path: str,
            target_relative_path: str,
        ) -> None:
            self.targets.append(target_relative_path)

    adapter = RecordingAdapter()

    async def principal() -> Principal:
        return PRINCIPAL

    async def database() -> AsyncSession:
        yield db

    async def protocol() -> RecordingAdapter:
        return adapter

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_chat_file_protocol_adapter] = protocol
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.patch(
                f"/api/v1/user/application-agents/{AGENT_ID}/chat-files/{record.file_id}",
                json={
                    "relative_path": (
                        f"workspace/sessions/{SESSION_ID}/{record.file_id}/../stolen.txt"
                    )
                },
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_chat_file_protocol_adapter, None)

    assert response.status_code == 200
    assert adapter.targets == [f"workspace/sessions/{SESSION_ID}/{record.file_id}/stolen.txt"]
    assert response.json()["display_name"] == "stolen.txt"
