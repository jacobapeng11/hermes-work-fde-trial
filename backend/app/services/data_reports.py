from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import logging
import time
from io import BytesIO
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import asyncio
import hashlib
import re
from sqlalchemy import select
from sqlglot import expressions as exp
from sqlglot import parse as parse_sql

from app.config import settings
from app.models import CloudHermesProfile
from app.services.auth import AuthenticatedUser
from app.services.knowledge_assertion import (
    KnowledgeAssertionIssuer,
    KnowledgeIdentity,
)
from app.services.runtime_knowledge import (
    RuntimeKnowledgeDownstreamClient,
    RuntimeKnowledgeError,
)
from app.services.runtime_workspace import Principal


logger = logging.getLogger(__name__)


class DataReportUnavailable(RuntimeError):
    code = "APP_CENTER_LIVE_DATA_UNAVAILABLE"
    error_code = -14009


class DataReportRateLimited(RuntimeError):
    code = "APP_CENTER_LIVE_DATA_RATE_LIMITED"
    error_code = -14010


class DataReportPackageInvalid(ValueError):
    pass


@dataclass(frozen=True)
class DataReportQuery:
    id: str
    sql: str
    page: int = 1
    page_size: int = 200


@dataclass(frozen=True)
class LiveManifest:
    engine: str
    data_mode: str
    queries: tuple[DataReportQuery, ...]


@dataclass(frozen=True)
class RenderedReport:
    html: bytes
    request_id: str


LIVE_MANIFEST_ENGINE = "sql_manifest_v1"
MAX_LIVE_QUERIES = 8
MAX_LIVE_SQL_LENGTH = 50_000
MAX_LIVE_RESULT_BYTES = 2 * 1024 * 1024
MAX_LIVE_HTML_ENTRY_BYTES = 16 * 1024 * 1024
_QUERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class _ManifestParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.fragments: list[str] = []
        self.capture = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {key.lower(): value for key, value in attrs}
        if (
            attributes.get("id") == "hermes-report-manifest"
            and attributes.get("type", "").lower() == "application/json"
        ):
            self.capture = True
            self.fragments.append("")

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.fragments[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self.capture = False


class _BodyEndParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.body_end_offset: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self.in_script = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized == "script":
            self.in_script = False
            return
        if normalized == "body" and not self.in_script and self.body_end_offset is None:
            self.body_end_offset = self.getpos()[1]


class _RateLimiter:
    def __init__(self, limit: int = 10, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.events: dict[tuple[int, int], list[float]] = defaultdict(list)

    def allow(self, key: tuple[int, int], now: float) -> bool:
        events = self.events[key]
        cutoff = now - self.window_seconds
        self.events[key] = [value for value in events if value > cutoff]
        if not self.events[key]:
            del self.events[key]
            return True
        events = self.events[key]
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


_shared_rate_limiter = _RateLimiter()
_shared_query_semaphore = asyncio.Semaphore(8)


def _normalized_entry_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("./")


def _is_html_entry(name: str) -> bool:
    return _normalized_entry_name(name).lower().endswith((".html", ".htm"))


def _read_html_entry(package: bytes, path: str = "index.html") -> str | None:
    try:
        with ZipFile(BytesIO(package)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            names = {
                _normalized_entry_name(item.filename): item for item in entries
            }
            entry = names.get(path)
            if (
                entry is None
                or entry.flag_bits & 0x1
                or entry.file_size > MAX_LIVE_HTML_ENTRY_BYTES
            ):
                return None
            return archive.read(entry).decode("utf-8")
    except (BadZipFile, OSError, UnicodeDecodeError, KeyError, RuntimeError):
        return None


def _manifest_fragment(html: str) -> str | None:
    parser = _ManifestParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    if len(parser.fragments) > 1:
        raise DataReportPackageInvalid("活数据页面只能包含一个 manifest")
    if not parser.fragments:
        return None
    return parser.fragments[0]


def _validate_query_sql(query: DataReportQuery) -> None:
    sql = query.sql.strip()
    if not sql or len(sql) > MAX_LIVE_SQL_LENGTH:
        raise DataReportPackageInvalid("活数据 SQL 长度无效")
    try:
        statements = parse_sql(sql, read="mysql")
    except Exception as exc:
        raise DataReportPackageInvalid("活数据 SQL 无法安全解析") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise DataReportPackageInvalid("活数据 SQL 只能是一条 SELECT")
    tree = statements[0]
    if tree.find(exp.Union) is not None or tree.find(exp.CTE) is not None:
        raise DataReportPackageInvalid("活数据 SQL 不支持 UNION 或 CTE")
    if not query.page >= 1:
        raise DataReportPackageInvalid("活数据查询页码无效")
    if not 1 <= query.page_size <= 500:
        raise DataReportPackageInvalid("活数据分页大小无效")


def _parse_manifest(fragment: str) -> LiveManifest:
    try:
        payload = json.loads(fragment)
    except (json.JSONDecodeError, TypeError) as exc:
        raise DataReportPackageInvalid("活数据报表 manifest 不是有效 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "engine",
        "data_mode",
        "queries",
    }:
        raise DataReportPackageInvalid("活数据报表 manifest 字段无效")
    engine = payload.get("engine")
    data_mode = payload.get("data_mode")
    raw_queries = payload.get("queries")
    if engine != LIVE_MANIFEST_ENGINE:
        raise DataReportPackageInvalid("活数据报表 engine 无效")
    if data_mode != "live":
        raise DataReportPackageInvalid("活数据报表 data_mode 无效")
    if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= MAX_LIVE_QUERIES:
        raise DataReportPackageInvalid("活数据查询数量必须在 1~8 条")
    queries: list[DataReportQuery] = []
    query_ids: set[str] = set()
    for raw_query in raw_queries:
        if not isinstance(raw_query, dict):
            raise DataReportPackageInvalid("活数据查询字段无效")
        query_fields = set(raw_query)
        if query_fields not in ({"id", "sql"}, {"id", "sql", "page", "page_size"}):
            raise DataReportPackageInvalid("活数据查询字段无效")
        query_id = raw_query.get("id")
        sql = raw_query.get("sql")
        page = raw_query.get("page")
        page_size = raw_query.get("page_size")
        if (
            not isinstance(query_id, str)
            or _QUERY_ID_PATTERN.fullmatch(query_id) is None
            or query_id in query_ids
        ):
            raise DataReportPackageInvalid("活数据 query id 无效或重复")
        if not isinstance(sql, str):
            raise DataReportPackageInvalid("活数据 SQL 无效")
        if page is None:
            page = 1
        if not isinstance(page, int) or isinstance(page, bool):
            raise DataReportPackageInvalid("活数据查询页码无效")
        if page_size is None:
            page_size = 200
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise DataReportPackageInvalid("活数据分页大小无效")
        query = DataReportQuery(
            id=query_id,
            sql=sql,
            page=page,
            page_size=page_size,
        )
        _validate_query_sql(query)
        query_ids.add(query_id)
        queries.append(query)
    return LiveManifest(
        engine=str(engine),
        data_mode=str(data_mode),
        queries=tuple(queries),
    )


def live_manifest_for_package(package: bytes) -> LiveManifest | None:
    """Return a valid frozen manifest, treating legacy invalid packages as static."""

    html = _read_html_entry(package)
    if html is None:
        return None
    try:
        fragment = _manifest_fragment(html)
    except DataReportPackageInvalid:
        return None
    if fragment is None:
        return None
    try:
        return _parse_manifest(fragment)
    except DataReportPackageInvalid:
        return None


def live_manifest_for_html_path(package: bytes, path: str) -> LiveManifest | None:
    html = _read_html_entry(package, path)
    if html is None:
        return None
    try:
        fragment = _manifest_fragment(html)
    except DataReportPackageInvalid:
        return None
    if fragment is None:
        return None
    try:
        return _parse_manifest(fragment)
    except DataReportPackageInvalid:
        return None


def validate_live_data_package(package: bytes) -> LiveManifest | None:
    html = _read_html_entry(package)
    if html is None:
        raise DataReportPackageInvalid("应用包根目录必须包含 index.html")
    fragment = _manifest_fragment(html)
    if fragment is None:
        root_manifest = None
    else:
        root_manifest = _parse_manifest(fragment)
    try:
        with ZipFile(BytesIO(package)) as archive:
            html_entries = [
                item
                for item in archive.infolist()
                if not item.is_dir() and _is_html_entry(item.filename)
            ]
    except (BadZipFile, OSError) as exc:
        raise DataReportPackageInvalid("活数据报表包无效") from exc

    query_ids: set[str] = set()
    query_count = 0
    if root_manifest is not None:
        query_count += len(root_manifest.queries)
        query_ids.update(query.id for query in root_manifest.queries)

    for item in html_entries:
        path = _normalized_entry_name(item.filename)
        if path == "index.html":
            continue
        page_html = _read_html_entry(package, path)
        if page_html is None:
            raise DataReportPackageInvalid("活数据页面无效")
        page_fragment = _manifest_fragment(page_html)
        if page_fragment is None:
            continue
        if root_manifest is None:
            raise DataReportPackageInvalid("活数据报表根入口缺少 manifest")
        page_manifest = _parse_manifest(page_fragment)
        query_count += len(page_manifest.queries)
        if query_count > MAX_LIVE_QUERIES:
            raise DataReportPackageInvalid("活数据查询数量必须在 1~8 条")
        for query in page_manifest.queries:
            if query.id in query_ids:
                raise DataReportPackageInvalid("活数据 query id 无效或重复")
            query_ids.add(query.id)

    if root_manifest is not None and query_count > MAX_LIVE_QUERIES:
        raise DataReportPackageInvalid("活数据查询数量必须在 1~8 条")
    return root_manifest


def _safe_json_script(payload: Mapping[str, object]) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        rendered.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def inject_report_data(html: bytes, payload: Mapping[str, object]) -> bytes:
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DataReportPackageInvalid("活数据报表 HTML 必须使用 UTF-8") from exc
    parser = _BodyEndParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise DataReportPackageInvalid("活数据报表 HTML 无法安全解析") from exc
    if parser.body_end_offset is None:
        raise DataReportPackageInvalid("活数据报表 HTML 缺少真实 body 结束标签")
    injection = (
        '<script type="application/json" id="hermes-report-data">'
        f"{_safe_json_script(payload)}</script>"
    )
    character_offset = parser.body_end_offset
    byte_offset = len(text[:character_offset].encode("utf-8"))
    return html[:byte_offset] + injection.encode("utf-8") + html[byte_offset:]


def _sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


class DataReportService:
    def __init__(
        self,
        db: object,
        *,
        downstream: RuntimeKnowledgeDownstreamClient | None = None,
        issuer: KnowledgeAssertionIssuer | None = None,
        rate_limiter: _RateLimiter | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.db = db
        self.downstream = downstream or RuntimeKnowledgeDownstreamClient()
        self.issuer = issuer or KnowledgeAssertionIssuer(
            issuer=settings.KNOWLEDGE_ASSERTION_ISSUER,
            ttl_seconds=settings.KNOWLEDGE_ASSERTION_TTL_SECONDS,
        )
        self.rate_limiter = rate_limiter or _shared_rate_limiter
        self.semaphore = semaphore or _shared_query_semaphore

    async def default_workspace_id(self, principal: Principal) -> int:
        result = await self.db.scalar(
            select(CloudHermesProfile.id)
            .where(
                CloudHermesProfile.org_id == principal.org_id,
                CloudHermesProfile.user_id == principal.user_id,
                CloudHermesProfile.profile_type == "default_chat_agent",
                CloudHermesProfile.status == "active",
            )
            .order_by(CloudHermesProfile.id)
            .limit(1)
        )
        if result is None:
            raise DataReportUnavailable("default workspace unavailable")
        return int(result)

    async def render_live_report(
        self,
        *,
        principal: Principal,
        user: AuthenticatedUser,
        app_id: int,
        package: bytes,
        html_path: str = "index.html",
    ) -> RenderedReport:
        manifest = live_manifest_for_html_path(package, html_path)
        if manifest is None:
            raise DataReportPackageInvalid("应用不是有效活数据报表")
        if not self.rate_limiter.allow((principal.user_id, app_id), time.monotonic()):
            raise DataReportRateLimited()

        request_id = uuid4().hex
        started = time.monotonic()
        if not user.user_code:
            raise DataReportUnavailable("user code unavailable")
        workspace_id = await self.default_workspace_id(principal)
        identity = KnowledgeIdentity(
            org_id=principal.org_id,
            user_id=principal.user_id,
            user_code=user.user_code or "",
            runtime_workspace_id=workspace_id,
            service_identity=False,
        )
        verify: bool | str = (
            settings.KNOWLEDGE_DATA_SERVICE_CA_FILE
            or settings.KNOWLEDGE_DATA_SERVICE_VERIFY_TLS
        )

        async def execute_query(
            query: DataReportQuery,
        ) -> tuple[str, dict[str, object], int]:
            query_started = time.monotonic()
            try:
                assertion = self.issuer.issue(
                    identity=identity,
                    audience=settings.DATA_SERVICE_ASSERTION_AUDIENCE,
                    scope="sql:query",
                    secret=settings.DATA_SERVICE_ASSERTION_SECRET,
                )
                async with self.semaphore:
                    result = await self.downstream.post_asserted(
                        base_url=settings.KNOWLEDGE_DATA_SERVICE_BASE_URL,
                        path="/v1/sql/query",
                        assertion=assertion,
                        payload={
                            "sql": query.sql,
                            "page": query.page,
                            "page_size": query.page_size,
                        },
                        timeout_seconds=min(
                            20, settings.KNOWLEDGE_SQL_QUERY_TIMEOUT_SECONDS
                        ),
                        verify=verify,
                        request_id=request_id,
                    )
            except RuntimeKnowledgeError as exc:
                logger.warning(
                    "live data report query failed request_id=%s app_id=%s "
                    "query=%s sql_hash=%s downstream_code=%s status=%s",
                    request_id,
                    app_id,
                    query.id,
                    _sql_hash(query.sql),
                    exc.code,
                    exc.status_code,
                )
                raise DataReportUnavailable() from exc
            except Exception as exc:
                logger.warning(
                    "live data report query failed request_id=%s app_id=%s "
                    "query=%s sql_hash=%s error_type=%s",
                    request_id,
                    app_id,
                    query.id,
                    _sql_hash(query.sql),
                    type(exc).__name__,
                )
                raise DataReportUnavailable() from exc
            if not isinstance(result, dict):
                raise DataReportUnavailable("invalid downstream response")
            row_count = result.get("row_count")
            normalized_rows = row_count if isinstance(row_count, int) else 0
            logger.info(
                "live data report query completed request_id=%s app_id=%s "
                "query=%s sql_hash=%s row_count=%d duration_ms=%.1f",
                request_id,
                app_id,
                query.id,
                _sql_hash(query.sql),
                normalized_rows,
                (time.monotonic() - query_started) * 1000,
            )
            return query.id, result, normalized_rows

        outcomes = await asyncio.gather(
            *(execute_query(query) for query in manifest.queries),
            return_exceptions=True,
        )
        results: dict[str, dict[str, object]] = {}
        result_rows = 0
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                if isinstance(outcome, DataReportUnavailable):
                    raise outcome
                raise DataReportUnavailable() from outcome
            query_id, result, row_count = outcome
            results[query_id] = result
            result_rows += row_count

        payload: dict[str, object] = {
            "request_id": request_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "user": {"name": user.name, "user_code": user.user_code},
            "queries": results,
        }
        serialized_report = _safe_json_script(payload)
        if len(serialized_report.encode("utf-8")) > MAX_LIVE_RESULT_BYTES:
            raise DataReportUnavailable("report result too large")
        html = _read_html_entry(package, html_path)
        if html is None:
            raise DataReportPackageInvalid("活数据报表包无效")
        rendered = inject_report_data(html.encode("utf-8"), payload)
        logger.info(
            "live data report completed request_id=%s app_id=%s page=%s engine=%s "
            "result_rows=%d result_bytes=%d duration_ms=%.1f",
            request_id,
            app_id,
            html_path,
            manifest.engine,
            result_rows,
            len(rendered),
            (time.monotonic() - started) * 1000,
        )
        return RenderedReport(html=rendered, request_id=request_id)
