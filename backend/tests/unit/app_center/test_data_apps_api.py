from __future__ import annotations

from io import BytesIO
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.auth import get_auth_token_service, get_platform_session_store
from app.api.v1.user.data_apps import (
    DATA_APP_CSP,
    get_app_center_service,
    get_data_report_service,
    launch_router,
    router,
)
from app.services.app_center_contracts import AppCenterError
from app.services.auth import AuthenticatedUser, AuthTokenService
from app.services.data_reports import RenderedReport
from app.services.platform_session import DataAppLaunch, PlatformSession
from app.services.runtime_workspace import Principal


MANIFEST = (
    '<script type="application/json" id="hermes-report-manifest">'
    '{"engine":"sql_manifest_v1","data_mode":"live","queries":['
    '{"id":"recent_output","sql":"SELECT region_name FROM crm_delivery_performance_t",'
    '"page":1,"page_size":100}]}</script>'
)
STATIC_HTML = "<!doctype html><html><body>snapshot</body></html>"
LIVE_HTML = f"<!doctype html><html><body>{MANIFEST}</body></html>"
PRINCIPAL = Principal(user_id=7, org_id=3)
USER = AuthenticatedUser(
    org_id=3,
    user_id=7,
    name="张三",
    email=None,
    user_code="000123",
)


def _package(html: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
    return output.getvalue()


def _multi_page_package() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", LIVE_HTML)
        archive.writestr(
            "pages/detail.html",
            f"<!doctype html><html><body>{MANIFEST}</body></html>",
        )
        archive.writestr("assets/app.js", "console.log('ok')")
    return output.getvalue()


class FakeAppService:
    def __init__(self, package: bytes, *, deny: bool = False) -> None:
        self.package_blob = package
        self.deny = deny

    async def public_package(self, *, app_id: int) -> bytes:
        return self.package_blob

    async def package(self, *, principal: Principal, app_id: int) -> tuple[str, bytes]:
        if self.deny:
            raise AppCenterError(
                "APP_CENTER_PERMISSION_DENIED",
                "你没有该应用的访问权限",
                status_code=403,
                error_code=-14004,
            )
        return "app.zip", self.package_blob

    async def consumer_package(self, *, principal: Principal, app_id: int) -> bytes:
        if self.deny:
            raise AppCenterError(
                "APP_CENTER_PERMISSION_DENIED",
                "你没有该应用的访问权限",
                status_code=403,
                error_code=-14004,
            )
        return self.package_blob

    async def require_consumer_access(self, *, principal: Principal, app_id: int) -> None:
        if self.deny:
            raise AppCenterError(
                "APP_CENTER_PERMISSION_DENIED",
                "你没有该应用的访问权限",
                status_code=403,
                error_code=-14004,
            )


class FakeSessionStore:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.consumed_tickets: list[str] = []
        self.created_sessions: list[PlatformSession] = []

    async def get(self, session_id: str) -> PlatformSession | None:
        if not self.valid:
            return None
        return PlatformSession(
            session_id=session_id,
            org_id=PRINCIPAL.org_id,
            user_id=PRINCIPAL.user_id,
            created_at=0,
        )

    async def touch(self, session_id: str) -> bool:
        return self.valid

    async def consume_data_app_launch(self, ticket: str) -> DataAppLaunch | None:
        if ticket != "ticket-1" or ticket in self.consumed_tickets:
            return None
        self.consumed_tickets.append(ticket)
        return DataAppLaunch(app_id=11, org_id=3, user_id=7)

    async def create(self, principal: Principal) -> PlatformSession:
        session = PlatformSession(
            session_id=f"browser-session-{len(self.created_sessions) + 1}",
            org_id=principal.org_id,
            user_id=principal.user_id,
            created_at=0,
        )
        self.created_sessions.append(session)
        return session


class FakeUserStore:
    async def get_user(self, *, org_id: int, user_id: int) -> AuthenticatedUser:
        assert (org_id, user_id) == (USER.org_id, USER.user_id)
        return USER


class FakeReportService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def render_live_report(
        self,
        *,
        principal: Principal,
        user: AuthenticatedUser,
        app_id: int,
        package: bytes,
        html_path: str = "index.html",
    ) -> RenderedReport:
        self.calls.append(
            {
                "principal": principal,
                "user": user,
                "app_id": app_id,
                "package": package,
                "html_path": html_path,
            }
        )
        return RenderedReport(
            html=b'<!doctype html><html><body>live</body></html>',
            request_id="request-1",
        )


def _client(
    *,
    package: bytes,
    deny: bool = False,
    session_valid: bool = True,
) -> tuple[TestClient, FakeReportService]:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1/user")
    report_service = FakeReportService()
    application.dependency_overrides[get_app_center_service] = lambda: FakeAppService(
        package,
        deny=deny,
    )
    application.dependency_overrides[get_data_report_service] = lambda: report_service
    application.dependency_overrides[get_platform_session_store] = lambda: FakeSessionStore(
        valid=session_valid
    )
    application.dependency_overrides[get_auth_token_service] = lambda: AuthTokenService(
        FakeUserStore()
    )
    return TestClient(application), report_service


def _launch_client(*, deny: bool = False) -> tuple[TestClient, FakeSessionStore]:
    application = FastAPI()
    application.include_router(launch_router, prefix="/api/v1/user")
    session_store = FakeSessionStore()
    application.dependency_overrides[get_app_center_service] = lambda: FakeAppService(
        _package(LIVE_HTML),
        deny=deny,
    )
    application.dependency_overrides[get_platform_session_store] = lambda: session_store
    application.dependency_overrides[get_auth_token_service] = lambda: AuthTokenService(
        FakeUserStore()
    )
    return TestClient(application), session_store


def test_static_package_redirects_without_requiring_session() -> None:
    client, reports = _client(package=_package(STATIC_HTML))

    response = client.get("/api/v1/user/data-apps/11/index.html", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/api/v1/app-center/11/index.html"
    assert reports.calls == []


def test_static_package_redirects_secondary_path_to_legacy_route() -> None:
    package = BytesIO()
    with ZipFile(package, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", STATIC_HTML)
        archive.writestr("pages/detail.html", STATIC_HTML)
    client, reports = _client(package=package.getvalue())

    response = client.get(
        "/api/v1/user/data-apps/11/pages/detail.html",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/api/v1/app-center/11/pages/detail.html"
    assert reports.calls == []


def test_live_package_injects_platform_return_link(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "PUBLIC_WEB_BASE", "https://platform.example")
    client, _reports = _client(package=_package(LIVE_HTML))
    client.cookies.set("hermes_platform_session", "session-1", path="/api/v1")

    response = client.get("/api/v1/user/data-apps/11/index.html")

    assert response.status_code == 200
    assert 'href="https://platform.example"' in response.text
    assert 'target="_top"' in response.text
    assert 'data-app-center-platform-link' in response.text
    assert "返回平台" in response.text
    assert response.text.index("返回平台") > response.text.index("<body>")
    assert response.text.index("返回平台") < response.text.index("live")
    assert response.headers["content-security-policy"] == (
        "sandbox allow-scripts allow-top-navigation-by-user-activation; "
        "default-src 'none'; script-src 'unsafe-inline' 'self'; "
        "style-src 'unsafe-inline' 'self'; img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'; "
        "frame-src 'none'; worker-src 'none'; media-src 'none'; manifest-src 'none'"
    )


def test_live_package_expired_session_redirects_to_authentication_handoff() -> None:
    client, reports = _client(package=_package(LIVE_HTML), session_valid=False)

    response = client.get(
        "/api/v1/user/data-apps/11/index.html",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/user/data-app-auth?return_to=%2Fapi%2Fv1%2Fuser%2Fdata-apps%2F11%2Findex.html"
    )
    assert response.headers["cache-control"] == "no-store"
    assert 'hermes_platform_session=""' in response.headers["set-cookie"]
    assert reports.calls == []


def test_live_package_renders_with_session_and_strict_csp() -> None:
    client, reports = _client(package=_package(LIVE_HTML))
    client.cookies.set("hermes_platform_session", "session-1", path="/api/v1")

    response = client.get("/api/v1/user/data-apps/11/index.html")

    assert response.status_code == 200
    assert "live" in response.text
    assert response.text.count("返回平台") == 1
    assert response.headers["content-security-policy"] == DATA_APP_CSP
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-hermes-request-id"] == "request-1"
    assert reports.calls[0]["principal"] == PRINCIPAL
    assert reports.calls[0]["user"] == USER
    assert reports.calls[0]["app_id"] == 11


def test_live_package_serves_secondary_page_with_its_own_manifest() -> None:
    client, reports = _client(package=_multi_page_package())
    client.cookies.set("hermes_platform_session", "session-1", path="/api/v1")

    response = client.get("/api/v1/user/data-apps/11/pages/detail.html")

    assert response.status_code == 200
    assert "返回平台" in response.text
    assert reports.calls[0]["html_path"] == "pages/detail.html"


def test_live_package_serves_local_asset_after_authorization() -> None:
    client, reports = _client(package=_multi_page_package())
    client.cookies.set("hermes_platform_session", "session-1", path="/api/v1")

    response = client.get("/api/v1/user/data-apps/11/assets/app.js")

    assert response.status_code == 200
    assert response.text == "console.log('ok')"
    assert response.headers["content-type"].startswith("text/javascript")
    assert reports.calls == []


def test_live_package_rejects_missing_local_entry() -> None:
    client, reports = _client(package=_multi_page_package())
    client.cookies.set("hermes_platform_session", "session-1", path="/api/v1")

    response = client.get("/api/v1/user/data-apps/11/assets/missing.js")

    assert response.status_code == 404
    assert reports.calls == []


def test_live_package_checks_application_visibility() -> None:
    client, reports = _client(package=_package(LIVE_HTML), deny=True)
    client.cookies.set("hermes_platform_session", "session-1", path="/api/v1")

    response = client.get("/api/v1/user/data-apps/11/index.html")

    assert response.status_code == 403
    assert response.headers["content-security-policy"] == DATA_APP_CSP
    assert reports.calls == []


def test_browser_launch_redemption_sets_an_independent_session_and_redirects() -> None:
    client, sessions = _launch_client()

    response = client.get(
        "/api/v1/user/data-app-launch/redeem?ticket=ticket-1",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/api/v1/user/data-apps/11/index.html"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    assert "hermes_platform_session=browser-session-1" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v1" in cookie
    assert len(sessions.created_sessions) == 1

    replay = client.get(
        "/api/v1/user/data-app-launch/redeem?ticket=ticket-1",
        follow_redirects=False,
    )
    assert replay.status_code == 401
    assert "set-cookie" not in replay.headers
    assert len(sessions.created_sessions) == 1


def test_browser_launch_redemption_rechecks_application_permission() -> None:
    client, sessions = _launch_client(deny=True)

    response = client.get(
        "/api/v1/user/data-app-launch/redeem?ticket=ticket-1",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert sessions.created_sessions == []


def test_browser_launch_redemption_rejects_an_unknown_ticket() -> None:
    client, sessions = _launch_client()
    client.cookies.set("hermes_platform_session", "existing-session", path="/api/v1")

    response = client.get(
        "/api/v1/user/data-app-launch/redeem?ticket=unknown",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert sessions.created_sessions == []
