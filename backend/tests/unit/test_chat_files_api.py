from fastapi.testclient import TestClient

from app.api.v1.user.chat_files import (
    InMemoryChatFileStore,
    get_chat_file_runtime_service,
)
from app.api.v1.user.runtime_workspace import get_principal
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.main import app
from app.services.chat_files import RegisterChatFileCommand
from app.services.runtime_ticket_issuer import RuntimeTicketIssuer
from app.services.runtime_workspace import Principal
from app.config import settings


_TICKET_SECRET = "runtime-file-ticket-test-secret-0001"


class _RuntimeService:
    async def get_runtime_endpoint(self, **_kwargs) -> RuntimeEndpoint:
        return RuntimeEndpoint(
            runtime_workspace_id=1,
            runtime_id="runtime-file-test",
            endpoint_base_url="http://runtime.invalid",
            endpoint_host_header="runtime-file-test.example",
            api_key=_TICKET_SECRET,
        )


async def test_in_memory_chat_file_store_upserts_session_path() -> None:
    store = InMemoryChatFileStore()
    first = await store.register_file(
        org_id=1,
        user_id=1,
        command=RegisterChatFileCommand(
            runtime_workspace_id=1,
            runtime_session_id="rs-1-1",
            runtime_run_id="rr-first",
            relative_path="workspace/sessions/rs-1-1/lesson.pptx",
            mime_type="application/old",
            size_bytes=100,
            source_type="tool",
        ),
    )
    second = await store.register_file(
        org_id=1,
        user_id=1,
        command=RegisterChatFileCommand(
            runtime_workspace_id=1,
            runtime_session_id="rs-1-1",
            runtime_run_id="rr-second",
            relative_path="workspace/sessions/rs-1-1/lesson.pptx",
            mime_type="application/pptx",
            size_bytes=200,
            source_type="tool",
        ),
    )

    listed = await store.list_files(
        org_id=1,
        user_id=1,
        runtime_workspace_id=1,
        runtime_session_id="rs-1-1",
    )

    assert first.file_id == second.file_id
    assert len(listed) == 1
    assert listed[0].runtime_run_id == "rr-second"
    assert listed[0].mime_type == "application/pptx"
    assert listed[0].size_bytes == 200


def test_chat_files_api_register_list_download_rename_delete_contract() -> None:
    client = TestClient(app)

    create_response = client.post(
        "/api/v1/user/chat-files",
        json={
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-1-1",
            "runtime_run_id": "rr-1-rs-1-1-1",
            "relative_path": "workspace/uploads/smoke.txt",
            "mime_type": "text/plain",
            "size_bytes": 12,
            "source_type": "upload",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["runtime_workspace_id"] == 1
    assert created["runtime_session_id"] == "rs-1-1"
    assert created["runtime_run_id"] == "rr-1-rs-1-1-1"
    assert created["relative_path"] == "workspace/sessions/rs-1-1/uploads/smoke.txt"
    assert created["display_name"] == "smoke.txt"
    assert "workspace_id" not in created
    assert "copaw" not in create_response.text

    list_response = client.get(
        "/api/v1/user/chat-files",
        params={"runtime_workspace_id": 1, "runtime_session_id": "rs-1-1"},
    )
    assert list_response.status_code == 200
    assert any(item["file_id"] == created["file_id"] for item in list_response.json())

    download_response = client.get(f"/api/v1/user/chat-files/{created['file_id']}/download")
    assert download_response.status_code == 200
    assert download_response.json()["relative_path"] == "workspace/sessions/rs-1-1/uploads/smoke.txt"

    rename_response = client.patch(
        f"/api/v1/user/chat-files/{created['file_id']}",
        json={"relative_path": "workspace/exports/smoke-renamed.txt"},
    )
    assert rename_response.status_code == 200
    assert rename_response.json()["display_name"] == "smoke-renamed.txt"

    delete_response = client.delete(f"/api/v1/user/chat-files/{created['file_id']}")
    assert delete_response.status_code == 200
    assert delete_response.json()["relative_path"] == "workspace/sessions/rs-1-1/exports/smoke-renamed.txt"

    missing_response = client.get(f"/api/v1/user/chat-files/{created['file_id']}/download")
    assert missing_response.status_code == 404


def test_chat_files_api_retires_backend_file_body_upload() -> None:
    response = TestClient(app).post(
        "/api/v1/user/chat-files/upload",
        params={"runtime_workspace_id": 1, "runtime_session_id": "rs-1-1"},
        headers={"X-File-Name": "legacy.txt", "Content-Type": "text/plain"},
        content=b"must not be buffered by Backend",
    )

    assert response.status_code == 410
    assert response.json() == {
        "detail": "use /chat-files/upload-intent and upload directly to Runtime"
    }


def test_chat_files_api_issues_scoped_runtime_upload_ticket() -> None:
    app.dependency_overrides[get_chat_file_runtime_service] = (
        lambda: _RuntimeService()
    )
    try:
        response = TestClient(app).post(
            "/api/v1/user/chat-files/upload-intent",
            json={
                "runtime_workspace_id": 1,
                "runtime_session_id": "rs-1-file-ticket",
                "display_name": "季度数据.csv",
                "mime_type": "text/csv",
                "size_bytes": 22,
            },
        )
    finally:
        app.dependency_overrides.pop(get_chat_file_runtime_service, None)

    assert response.status_code == 200
    intent = response.json()
    assert intent["runtime_path"] == (
        "/runtime-files/runtime-file-test/upload"
    )
    claims = RuntimeTicketIssuer(
        runtime_id="runtime-file-test",
        secret=_TICKET_SECRET,
    ).verify_file_transfer(
        intent["file_transfer_ticket"],
        operation="upload",
    )
    assert claims.session_id == "rs-1-file-ticket"
    assert claims.file_id == intent["file"]["file_id"]
    assert claims.relative_path.endswith("/季度数据.csv")
    assert claims.max_bytes == 22


def test_chat_files_api_issues_download_ticket_without_proxying_body() -> None:
    client = TestClient(app)
    created = client.post(
        "/api/v1/user/chat-files",
        json={
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-download-ticket",
            "relative_path": "workspace/uploads/report.txt",
            "mime_type": "text/plain",
            "size_bytes": 12,
            "source_type": "upload",
        },
    ).json()
    app.dependency_overrides[get_chat_file_runtime_service] = (
        lambda: _RuntimeService()
    )
    try:
        response = client.post(
            f"/api/v1/user/chat-files/{created['file_id']}/download-intent"
        )
    finally:
        app.dependency_overrides.pop(get_chat_file_runtime_service, None)

    assert response.status_code == 200
    intent = response.json()
    assert intent["runtime_path"] == (
        "/runtime-files/runtime-file-test/download"
    )
    claims = RuntimeTicketIssuer(
        runtime_id="runtime-file-test",
        secret=_TICKET_SECRET,
    ).verify_file_transfer(
        intent["file_transfer_ticket"],
        operation="download",
    )
    assert claims.file_id == created["file_id"]
    assert claims.max_bytes == 12

    legacy = client.get(
        f"/api/v1/user/chat-files/{created['file_id']}/content"
    )
    assert legacy.status_code == 410


def test_chat_files_api_creates_signed_read_only_office_preview_session(monkeypatch) -> None:
    client = TestClient(app)
    created = client.post(
        "/api/v1/user/chat-files",
        json={
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-office",
            "relative_path": "workspace/uploads/quarterly-report.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size_bytes": 4096,
            "source_type": "upload",
        },
    ).json()
    monkeypatch.setattr(settings, "ONLYOFFICE_PUBLIC_URL", "http://office.example.test")
    monkeypatch.setattr(settings, "ONLYOFFICE_INTERNAL_API_BASE", "http://platform.internal:8011")

    response = client.post(f"/api/v1/user/chat-files/{created['file_id']}/office-preview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["document_server_url"] == "http://office.example.test"
    assert payload["config"]["documentType"] == "cell"
    assert payload["config"]["document"]["permissions"]["edit"] is False
    assert payload["config"]["editorConfig"]["mode"] == "view"
    assert payload["config"]["document"]["url"].startswith(
        f"http://platform.internal:8011/api/v1/user/chat-files/{created['file_id']}/office-content?token="
    )
    assert "relative_path" not in response.text

    invalid = client.get(
        f"/api/v1/user/chat-files/{created['file_id']}/office-content",
        params={"token": "invalid"},
    )
    assert invalid.status_code == 401


def test_chat_files_api_rejects_forbidden_visible_paths() -> None:
    client = TestClient(app)

    for relative_path in [
        "home/uploads/secret.txt",
        "workspace/.env",
        "workspace/config.yaml",
        "workspace/logs/runtime.log",
        "workspace/state.sqlite",
    ]:
        response = client.post(
            "/api/v1/user/chat-files",
            json={
                "runtime_workspace_id": 1,
                "relative_path": relative_path,
                "size_bytes": 1,
                "source_type": "upload",
            },
        )
        assert response.status_code == 400, relative_path


def test_chat_files_api_contract_isolates_files_by_platform_principal() -> None:
    client = TestClient(app)
    create_response = client.post(
        "/api/v1/user/chat-files",
        json={
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-1-1",
            "runtime_run_id": "rr-1-rs-1-1-1",
            "relative_path": "workspace/uploads/owner-only.txt",
            "mime_type": "text/plain",
            "size_bytes": 12,
            "source_type": "upload",
        },
    )
    assert create_response.status_code == 200
    file_id = create_response.json()["file_id"]

    async def other_principal() -> Principal:
        return Principal(org_id=1, user_id=2)

    app.dependency_overrides[get_principal] = other_principal
    try:
        other_client = TestClient(app)
        list_response = other_client.get(
            "/api/v1/user/chat-files",
            params={"runtime_workspace_id": 1, "runtime_session_id": "rs-1-1"},
        )
        download_response = other_client.get(f"/api/v1/user/chat-files/{file_id}/download")
        delete_response = other_client.delete(f"/api/v1/user/chat-files/{file_id}")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert list_response.status_code == 404
    assert list_response.json() == {"detail": "runtime workspace not found"}
    assert download_response.status_code == 404
    assert download_response.json() == {"detail": "chat file not found"}
    assert delete_response.status_code == 404
    assert delete_response.json() == {"detail": "chat file not found"}
