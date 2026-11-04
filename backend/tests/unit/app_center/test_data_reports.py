from __future__ import annotations

import asyncio
from io import BytesIO
import json
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import jwt
import pytest

from app.config import settings
from app.services.auth import AuthenticatedUser
from app.services.data_reports import (
    DataReportPackageInvalid,
    DataReportRateLimited,
    DataReportService,
    LiveManifest,
    inject_report_data,
    live_manifest_for_package,
    validate_live_data_package,
)
from app.services.runtime_workspace import Principal


def _manifest_payload(
    queries: list[dict[str, object]],
    **overrides: object,
) -> str:
    payload: dict[str, object] = {
        "engine": "sql_manifest_v1",
        "data_mode": "live",
        "queries": queries,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _manifest(queries: list[dict[str, object]] | None = None) -> str:
    normalized = queries or [
        {
            "id": "recent_output",
            "sql": (
                "SELECT region_name, SUM(amount) AS amount_wan "
                "FROM crm_delivery_performance_t GROUP BY region_name"
            ),
            "page": 1,
            "page_size": 200,
        }
    ]
    return (
        '<script type="application/json" id="hermes-report-manifest">'
        f"{_manifest_payload(normalized)}</script>"
    )


def _package(html: str, *, extra_file: bool = False) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
        if extra_file:
            archive.writestr("app.js", "console.log('ok')")
    return output.getvalue()


def _multi_page_package(
    *,
    root_html: str,
    detail_html: str | None = None,
) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", root_html)
        archive.writestr("pages/detail.html", detail_html or "<!doctype html><html><body>shell</body></html>")
        archive.writestr("assets/app.js", "console.log('ok')")
        archive.writestr("assets/style.css", "body{color:red}")
    return output.getvalue()


def test_live_sql_manifest_is_parsed_from_index_html() -> None:
    package = _package(f"<!doctype html><html><body>{_manifest()}</body></html>")

    manifest = validate_live_data_package(package)

    assert manifest == live_manifest_for_package(package)
    assert isinstance(manifest, LiveManifest)
    assert manifest.engine == "sql_manifest_v1"
    assert manifest.queries[0].id == "recent_output"


@pytest.mark.parametrize(
    "payload",
    [
        {"engine": "unknown", "data_mode": "live", "queries": []},
        {"engine": "sql_manifest_v1", "data_mode": "snapshot", "queries": []},
        {"engine": "sql_manifest_v1", "data_mode": "live"},
        {
            "engine": "sql_manifest_v1",
            "data_mode": "live",
            "queries": [
                {
                    "id": "bad-sql",
                    "sql": "DELETE FROM crm_project_info_t",
                    "page": 1,
                    "page_size": 100,
                }
            ],
        },
        {
            "engine": "sql_manifest_v1",
            "data_mode": "live",
            "queries": [
                {
                    "id": "union",
                    "sql": "SELECT 1 AS a UNION SELECT 2 AS a",
                    "page": 1,
                    "page_size": 100,
                }
            ],
        },
        {
            "engine": "sql_manifest_v1",
            "data_mode": "live",
            "queries": [
                {
                    "id": "page-size",
                    "sql": "SELECT region_name FROM crm_project_info_t",
                    "page": 1,
                    "page_size": 501,
                }
            ],
        },
        {
            "engine": "sql_manifest_v1",
            "data_mode": "live",
            "queries": [
                {
                    "id": "same-id",
                    "sql": "SELECT region_name FROM crm_project_info_t",
                    "page": 1,
                    "page_size": 100,
                },
                {
                    "id": "same-id",
                    "sql": "SELECT project_status_name FROM crm_project_info_t",
                    "page": 1,
                    "page_size": 100,
                },
            ],
        },
    ],
)
def test_invalid_live_manifest_is_rejected(payload: dict[str, object]) -> None:
    queries = payload.get("queries", [])
    assert isinstance(queries, list)
    fragment = _manifest_payload(
        queries,
        engine=payload.get("engine", "sql_manifest_v1"),
        data_mode=payload.get("data_mode", "live"),
    )
    html = (
        "<!doctype html><html><body>"
        '<script type="application/json" id="hermes-report-manifest">'
        f"{fragment}</script></body></html>"
    )
    package = _package(html)

    with pytest.raises(DataReportPackageInvalid):
        validate_live_data_package(package)
    assert live_manifest_for_package(package) is None


def test_live_manifest_rejects_too_many_queries() -> None:
    queries = [
        {
            "id": f"query-{index}",
            "sql": f"SELECT region_name AS region_{index} FROM crm_project_info_t",
            "page": 1,
            "page_size": 10,
        }
        for index in range(9)
    ]
    html = f"<!doctype html><html><body>{_manifest(queries)}</body></html>"

    with pytest.raises(DataReportPackageInvalid):
        validate_live_data_package(_package(html))


def test_live_query_pagination_has_safe_defaults() -> None:
    queries = [
        {
            "id": "recent_output",
            "sql": "SELECT region_name FROM crm_delivery_performance_t",
        }
    ]
    html = f"<!doctype html><html><body>{_manifest(queries)}</body></html>"

    manifest = validate_live_data_package(_package(html))

    assert manifest is not None
    assert manifest.queries[0].page == 1
    assert manifest.queries[0].page_size == 200


def test_live_package_accepts_local_assets() -> None:
    html = f"<!doctype html><html><body>{_manifest()}</body></html>"

    manifest = validate_live_data_package(_package(html, extra_file=True))

    assert manifest is not None
    assert manifest.queries[0].id == "recent_output"


def test_live_package_accepts_page_manifest_and_static_shell() -> None:
    root_html = f"<!doctype html><html><body>{_manifest()}</body></html>"
    detail_query = {
        "id": "detail_output",
        "sql": "SELECT office_name FROM crm_delivery_performance_t",
    }
    detail_html = (
        "<!doctype html><html><body>"
        f"{_manifest([detail_query])}</body></html>"
    )
    package = _multi_page_package(root_html=root_html, detail_html=detail_html)
    shell_package = _multi_page_package(root_html=root_html)

    root_manifest = validate_live_data_package(package)
    shell_manifest = validate_live_data_package(shell_package)

    assert root_manifest is not None
    assert root_manifest.queries[0].id == "recent_output"
    assert shell_manifest == root_manifest


def test_live_package_rejects_duplicate_page_query_id() -> None:
    root_html = f"<!doctype html><html><body>{_manifest()}</body></html>"
    detail_html = f"<!doctype html><html><body>{_manifest()}</body></html>"

    with pytest.raises(DataReportPackageInvalid):
        validate_live_data_package(_multi_page_package(root_html=root_html, detail_html=detail_html))


def test_live_package_rejects_manifest_without_root_marker() -> None:
    root_html = "<!doctype html><html><body>snapshot</body></html>"
    detail_html = f"<!doctype html><html><body>{_manifest()}</body></html>"

    with pytest.raises(DataReportPackageInvalid, match="根入口"):
        validate_live_data_package(_multi_page_package(root_html=root_html, detail_html=detail_html))


def test_report_data_is_injected_into_real_body_end() -> None:
    html = (
        "<!doctype html><html><head><script>const fake = '</body>';</script></head>"
        f"<body><main>报表</main>{_manifest()}</body></html>"
    ).encode("utf-8")

    rendered = inject_report_data(html, {"user": {"name": "张<三>"}})
    text = rendered.decode("utf-8")

    assert text.index('id="hermes-report-data"') > text.index("<main>报表</main>")
    assert text.index('id="hermes-report-data"') < text.rindex("</body>")
    assert "\\u003c" in text


class FakeDownstream:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post_asserted(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "row_count": 1,
            "total": 1,
            "page": 1,
            "page_size": 200,
            "total_pages": 1,
            "data": [{"count": len(self.calls)}],
            "semantic_warnings": [],
        }


@pytest.mark.asyncio
async def test_render_uses_frozen_sql_and_current_user_identity(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_SERVICE_ASSERTION_SECRET", "test-secret")
    monkeypatch.setattr(settings, "KNOWLEDGE_DATA_SERVICE_BASE_URL", "http://data.example")
    downstream = FakeDownstream()
    service = DataReportService(
        object(),
        downstream=downstream,  # type: ignore[arg-type]
        rate_limiter=type("Limiter", (), {"allow": staticmethod(lambda *_: True)})(),
        semaphore=asyncio.Semaphore(2),
    )

    async def workspace(_principal: Principal) -> int:
        return 99

    monkeypatch.setattr(service, "default_workspace_id", workspace)
    queries = [
        {
            "id": "recent_output",
            "sql": (
                "SELECT region_name, SUM(amount) AS amount_wan "
                "FROM crm_delivery_performance_t GROUP BY region_name"
            ),
            "page": 1,
            "page_size": 200,
        },
        {
            "id": "recent_opportunity",
            "sql": (
                "SELECT region_name, COUNT(*) AS cnt "
                "FROM crm_project_info_t GROUP BY region_name"
            ),
            "page": 2,
            "page_size": 500,
        },
    ]
    package = _package(f"<!doctype html><html><body>{_manifest(queries)}</body></html>")

    rendered = await service.render_live_report(
        principal=Principal(user_id=7, org_id=3),
        user=AuthenticatedUser(
            org_id=3,
            user_id=7,
            name="张三",
            email=None,
            user_code="000123",
        ),
        app_id=11,
        package=package,
    )

    assert len(downstream.calls) == 2
    assert {call["payload"]["sql"] for call in downstream.calls} == {
        str(query["sql"]) for query in queries
    }
    assertion = downstream.calls[0]["assertion"]
    claims = jwt.decode(
        assertion,
        "test-secret",
        algorithms=["HS256"],
        audience=settings.DATA_SERVICE_ASSERTION_AUDIENCE,
    )
    assert claims["sub"] == "7"
    assert claims["org_id"] == 3
    assert claims["user_code"] == "000123"
    assert claims["runtime_workspace_id"] == 99
    assert claims["scope"] == "sql:query"
    assert "service_identity" not in claims
    rendered_html = rendered.html.decode("utf-8")
    assert '"name":"张三"' in rendered_html
    assert '"recent_output"' in rendered_html


@pytest.mark.asyncio
async def test_render_applies_per_user_per_app_rate_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_SERVICE_ASSERTION_SECRET", "test-secret")
    service = DataReportService(
        object(),
        downstream=FakeDownstream(),  # type: ignore[arg-type]
        rate_limiter=type("Limiter", (), {"allow": staticmethod(lambda *_: False)})(),
        semaphore=asyncio.Semaphore(1),
    )

    with pytest.raises(DataReportRateLimited):
        await service.render_live_report(
            principal=Principal(user_id=7, org_id=3),
            user=AuthenticatedUser(
                org_id=3,
                user_id=7,
                name="张三",
                email=None,
                user_code="000123",
            ),
            app_id=11,
            package=_package(f"<!doctype html><html><body>{_manifest()}</body></html>"),
        )
