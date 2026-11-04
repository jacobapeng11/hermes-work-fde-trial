from __future__ import annotations

from io import BytesIO
from urllib.parse import parse_qs, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.auth import get_platform_session_store
from app.api.v1.user.apps import (
    get_app_center_service,
    get_principal,
    router,
)
from app.schemas.app_center import AppCenterListItem
from app.services.app_center_contracts import AppCenterError
from app.services.platform_session import InMemoryPlatformSessionStore
from app.services.runtime_workspace import Principal


PRINCIPAL = Principal(user_id=7, org_id=3)
HTML = "<!doctype html><html><body>ok</body></html>"


def item(**overrides: object) -> AppCenterListItem:
    values: dict[str, object] = {
        "id": 11,
        "name": "看板",
        "description": "看板数据概览",
        "access_url": "https://demo.example/11",
        "source_type": "agent_html",
        "share_mode": "scope",
        "creator_name": "创建者",
        "mine": True,
        "updated_at": "2026-10-16T12:00:00",
        "scope_summary": "全员",
        "publish_version_id": "3567",
    }
    values.update(overrides)
    return AppCenterListItem.model_validate(values)


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def create_from_html(self, **kwargs: object) -> AppCenterListItem:
        self.calls.append(("create_from_html", (), kwargs))
        return item()

    async def create_from_package(self, **kwargs: object) -> AppCenterListItem:
        self.calls.append(("create_from_package", (), kwargs))
        return item(source_type="manual_zip")

    async def list_for_user(self, **_: object) -> list[AppCenterListItem]:
        return [item()]

    async def require_consumer_access(self, **kwargs: object) -> None:
        self.calls.append(("require_consumer_access", (), kwargs))

    async def update_from_package(self, **kwargs: object) -> AppCenterListItem:
        self.calls.append(("update_from_package", (), kwargs))
        return item(publish_version_id="3569")


class FailingService(FakeService):
    async def create_from_html(self, **kwargs: object) -> AppCenterListItem:
        raise AppCenterError(
            "APP_CENTER_PACKAGE_INVALID",
            "应用包根目录必须包含 index.html",
            status_code=400,
            error_code=-14001,
        )


class DeniedService(FakeService):
    async def require_consumer_access(self, **kwargs: object) -> None:
        raise AppCenterError(
            "APP_CENTER_PERMISSION_DENIED",
            "你没有该应用的访问权限",
            status_code=403,
            error_code=-14004,
        )


def _zip() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", HTML)
    return output.getvalue()


def _client(
    service: FakeService,
    session_store: InMemoryPlatformSessionStore | None = None,
) -> TestClient:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1/user")
    application.dependency_overrides[get_principal] = lambda: PRINCIPAL
    application.dependency_overrides[get_app_center_service] = lambda: service
    application.dependency_overrides[get_platform_session_store] = lambda: (
        session_store or InMemoryPlatformSessionStore()
    )
    return TestClient(application)


def test_create_agent_html_route_uses_service() -> None:
    service = FakeService()
    response = _client(service).post(
        "/api/v1/user/apps",
        json={
            "name": "看板",
            "description": "看板数据概览",
            "html_content": HTML,
            "share_mode": "scope",
            "idempotency_key": "request-1",
            "scope": {"organization": True, "user_ids": [], "department_ids": []},
            "source_session_id": "session-1",
            "source_run_id": "run-1",
        },
    )

    assert response.status_code == 201
    assert response.json()["access_url"] == "https://demo.example/11"
    assert service.calls[0][2]["idempotency_key"] == "request-1"


def test_upload_route_parses_metadata_and_file() -> None:
    service = FakeService()
    metadata = (
        '{"name":"手工应用","description":"手工静态应用","share_mode":"link","idempotency_key":"request-2","source_session_id":"session-2","source_run_id":"run-2"}'
    )
    response = _client(service).post(
        "/api/v1/user/apps/upload",
        data={"metadata": metadata},
        files={"file": ("app.zip", _zip(), "application/zip")},
    )

    assert response.status_code == 201
    assert response.json()["source_type"] == "manual_zip"
    assert service.calls[0][2]["name"] == "手工应用"
    assert service.calls[0][2]["description"] == "手工静态应用"
    assert service.calls[0][2]["share_mode"] == "link"
    assert service.calls[0][2]["source_session_id"] == "session-2"
    assert service.calls[0][2]["source_run_id"] == "run-2"
    assert service.calls[0][2]["package"] == _zip()


def test_update_package_accepts_multipart_without_changing_route() -> None:
    service = FakeService()
    response = _client(service).put(
        "/api/v1/user/apps/11/package",
        files={"file": ("update.zip", _zip(), "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["publish_version_id"] == "3569"
    assert service.calls[0][2]["app_id"] == 11


def test_controlled_error_contains_numeric_code_and_request_id() -> None:
    response = _client(FailingService()).post(
        "/api/v1/user/apps",
        json={
            "name": "失败",
            "description": "失败场景",
            "html_content": HTML,
            "share_mode": "link",
        },
    )
    detail = response.json()["detail"]

    assert response.status_code == 400
    assert detail["code"] == "APP_CENTER_PACKAGE_INVALID"
    assert detail["error_code"] == -14001
    assert detail["request_id"]


async def test_browser_launch_issues_a_bound_one_time_ticket() -> None:
    service = FakeService()
    store = InMemoryPlatformSessionStore()

    response = _client(service, store).post("/api/v1/user/apps/11/browser-launch")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["expires_in"] == 60
    parsed = urlparse(payload["launch_url"])
    assert parsed.path == "/api/v1/user/data-app-launch/redeem"
    ticket = parse_qs(parsed.query)["ticket"][0]
    launch = await store.consume_data_app_launch(ticket)
    assert launch is not None
    assert launch.principal == PRINCIPAL
    assert launch.app_id == 11
    assert service.calls == [(
        "require_consumer_access",
        (),
        {"principal": PRINCIPAL, "app_id": 11},
    )]


def test_browser_launch_denial_does_not_issue_a_ticket() -> None:
    store = InMemoryPlatformSessionStore()

    response = _client(DeniedService(), store).post(
        "/api/v1/user/apps/11/browser-launch"
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "APP_CENTER_PERMISSION_DENIED"
    assert store.data_app_launches == {}
