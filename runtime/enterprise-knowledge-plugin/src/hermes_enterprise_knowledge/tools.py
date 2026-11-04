from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .context import enterprise_knowledge_root
from .platform_client import PlatformClient, PlatformClientError


ALLOWED_EXTENSIONS = {".md", ".markdown", ".yaml", ".yml"}
_SQL_TOOL_MAX_PAGE_SIZE = 100
_SQL_TOOL_MAX_RESULT_CHARS = 16_000


def _bounded_sql_query_params(params: dict[str, Any]) -> dict[str, Any]:
    payload = dict(params)
    try:
        page_size = int(payload.get("page_size", 100))
    except (TypeError, ValueError):
        page_size = 100
    payload["page_size"] = min(max(page_size, 1), _SQL_TOOL_MAX_PAGE_SIZE)
    return payload


def _bounded_sql_query_result(result: Any) -> Any:
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        return result

    payload = dict(result)
    rows = list(payload.get("data") or [])
    original_row_count = len(rows)
    payload["data"] = rows

    def serialized() -> str:
        return json.dumps(payload, ensure_ascii=False)

    while len(payload["data"]) > 1 and len(serialized()) > _SQL_TOOL_MAX_RESULT_CHARS:
        payload["data"] = payload["data"][:-1]

    if len(payload["data"]) < original_row_count:
        payload["original_row_count"] = original_row_count
        payload["row_count"] = len(payload["data"])
        payload["truncated"] = True
        page = int(payload.get("page") or 1)
        total_pages = int(payload.get("total_pages") or page)
        if page < total_pages:
            payload["next_page"] = page + 1
    return payload


def read_bundle_file(relative_path: str, *, root: Path | None = None) -> str:
    bundle_root = (root or enterprise_knowledge_root()).resolve()
    requested = Path(relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("invalid enterprise knowledge path")
    target = (bundle_root / requested).resolve(strict=True)
    if target.suffix.lower() not in ALLOWED_EXTENSIONS or not target.is_file():
        raise ValueError("unsupported enterprise knowledge file")
    if not target.is_relative_to(bundle_root):
        raise ValueError("enterprise knowledge path escapes bundle")
    return target.read_text(encoding="utf-8")


def _result(call) -> str:
    try:
        return json.dumps(call(), ensure_ascii=False)
    except PlatformClientError as exc:
        return json.dumps({"error": exc.as_error()}, ensure_ascii=False)
    except (OSError, UnicodeError, ValueError):
        return json.dumps(
            {
                "error": {
                    "code": "enterprise_knowledge_tool_failed",
                    "message": "企业知识工具无法完成该请求。",
                    "retryable": False,
                }
            },
            ensure_ascii=False,
        )


def _sql_result(call) -> str:
    try:
        return json.dumps(
            _bounded_sql_query_result(call()),
            ensure_ascii=False,
        )
    except PlatformClientError as exc:
        return json.dumps({"error": exc.as_error()}, ensure_ascii=False)
    except (OSError, UnicodeError, ValueError):
        return json.dumps(
            {
                "error": {
                    "code": "enterprise_knowledge_tool_failed",
                    "message": "企业知识工具无法完成该请求。",
                    "retryable": False,
                }
            },
            ensure_ascii=False,
        )


def handlers(client: PlatformClient | None = None) -> dict[str, Any]:
    client = client or PlatformClient()

    def read_when_enabled(
        relative_path: str,
        *,
        runtime_session_id: str | None,
        turn_id: str | None,
        tool_call_id: str | None,
    ) -> dict[str, str]:
        if client.state(
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ).get("enabled") is not True:
            raise PlatformClientError("enterprise knowledge is disabled")
        return {"content": read_bundle_file(relative_path)}

    return {
        "enterprise_knowledge_read": lambda params, **context: _result(lambda: read_when_enabled(str(params.get("relative_path", "")), runtime_session_id=context.get("session_id"), turn_id=context.get("turn_id"), tool_call_id=context.get("tool_call_id"))),
        "enterprise_sql_context": lambda params, **context: _result(lambda: client.post("/sql/context", {"query": params.get("query"), "domain": params.get("domain")}, runtime_session_id=context.get("session_id"), turn_id=context.get("turn_id"), tool_call_id=context.get("tool_call_id"))),
        "enterprise_sql_query": lambda params, **context: _sql_result(lambda: client.sql_query(_bounded_sql_query_params({"sql": params.get("sql"), "page": params.get("page", 1), "page_size": params.get("page_size", 100)}), runtime_session_id=context.get("session_id"), turn_id=context.get("turn_id"), tool_call_id=context.get("tool_call_id"))),
        "enterprise_document_search": lambda params, **context: _result(lambda: client.post("/documents/search", {"question": params.get("question"), "tag": params.get("tag")}, runtime_session_id=context.get("session_id"), turn_id=context.get("turn_id"), tool_call_id=context.get("tool_call_id"))),
    }


TOOL_SCHEMAS = {
    "enterprise_knowledge_read": {
        "description": "Read a Markdown or YAML file from the mounted enterprise knowledge bundle.",
        "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}}, "required": ["relative_path"]},
    },
    "enterprise_sql_context": {
        "description": "Retrieve relevant enterprise SQL schema, guidance, and examples.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "domain": {"type": ["string", "null"]}}, "required": ["query"]},
    },
    "enterprise_sql_query": {
        "description": "Execute a permission-filtered read-only enterprise SQL query.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}, "page": {"type": "integer", "minimum": 1}, "page_size": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["sql"]},
    },
    "enterprise_document_search": {
        "description": "Search enterprise documents visible to the current Runtime user.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}, "tag": {"type": ["string", "null"]}}, "required": ["question"]},
    },
}
