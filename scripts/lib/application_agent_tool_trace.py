from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


REDACTED = "••••"
TRUNCATED = "[内容已截断]"
MAX_DEPTH = 8
DEFAULT_TERMINAL_LIMIT = 4_000

_SENSITIVE_KEY_PARTS = (
    "token",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "credential",
    "assertion",
    "privatekey",
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\b"
)
_API_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_text(value: str) -> str:
    text = _BEARER_RE.sub(REDACTED, value)
    text = _JWT_RE.sub(REDACTED, text)
    return _API_KEY_RE.sub(REDACTED, text)


def parse_recorded_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    candidate = value.strip()
    if not candidate or candidate[0] not in "[{\"":
        return value
    try:
        return json.loads(candidate)
    except (TypeError, json.JSONDecodeError):
        return value


def sanitize_tool_value(value: object, *, depth: int = 0) -> object:
    if depth >= MAX_DEPTH:
        return "[内容层级过深]"
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if _sensitive_key(key)
                else sanitize_tool_value(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_tool_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _mapping(value: object) -> Mapping[str, Any]:
    parsed = parse_recorded_value(value)
    return parsed if isinstance(parsed, Mapping) else {}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(_redact_text(value).split())
    if not cleaned:
        return None
    return cleaned[:160] + ("…" if len(cleaned) > 160 else "")


def _length(value: object) -> int | None:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (Mapping, list, tuple)):
        return len(json.dumps(value, ensure_ascii=False, default=str))
    return None


def _first_text(arguments: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_names(arguments: Mapping[str, Any]) -> list[str]:
    direct = _first_text(arguments, "name", "tool_name", "tool")
    if direct:
        return [_text(direct) or "未知工具"]
    for key in ("names", "tool_names", "tools"):
        value = arguments.get(key)
        if isinstance(value, list):
            return [safe for item in value if (safe := _text(item))]
    return []


def summarize_tool_call(name: str, input_value: object, result_value: object) -> str:
    arguments = _mapping(input_value)
    if name == "tool_describe":
        names = _tool_names(arguments)
        return (
            f"查看工具说明：{'、'.join(names)}"
            if names
            else "查看工具的使用说明"
        )
    if name == "enterprise_knowledge_read":
        path = _text(_first_text(arguments, "relative_path", "path"))
        return f"读取企业知识文件 {path}" if path else "读取企业知识文件"
    if name == "enterprise_sql_context":
        query_chars = _length(arguments.get("query"))
        domain = _text(arguments.get("domain"))
        details = []
        if query_chars is not None:
            details.append(f"问题 {query_chars} 字符")
        if domain:
            details.append(f"业务域 {domain}")
        suffix = f"（{'，'.join(details)}）" if details else ""
        return f"检索企业 SQL 上下文{suffix}"
    if name == "enterprise_sql_query":
        sql_chars = _length(arguments.get("sql"))
        details = [f"SQL {sql_chars} 字符"] if sql_chars is not None else []
        if isinstance(arguments.get("page"), int):
            details.append(f"第 {arguments['page']} 页")
        if isinstance(arguments.get("page_size"), int):
            details.append(f"每页 {arguments['page_size']} 条")
        suffix = f"（{'，'.join(details)}）" if details else ""
        return f"执行权限过滤的只读 SQL{suffix}"
    if name == "enterprise_document_search":
        question_chars = _length(arguments.get("question"))
        tag = _text(arguments.get("tag"))
        details = []
        if question_chars is not None:
            details.append(f"问题 {question_chars} 字符")
        if tag:
            details.append(f"标签 {tag}")
        suffix = f"（{'，'.join(details)}）" if details else ""
        return f"搜索当前用户可见的企业文档{suffix}"
    if name == "write_file":
        path = _text(_first_text(arguments, "path", "file_path", "filename"))
        content_chars = _length(arguments.get("content"))
        target = f" {path}" if path else ""
        suffix = f"（内容 {content_chars} 字符）" if content_chars is not None else ""
        return f"写入文件{target}{suffix}"
    if name == "read_file":
        path = _text(_first_text(arguments, "path", "file_path", "filename"))
        return f"读取文件 {path}" if path else "读取文件"

    if arguments:
        keys = [str(key) for key in list(arguments)[:8] if not _sensitive_key(key)]
        if keys:
            return f"调用工具（输入字段 {'、'.join(keys)}）"
    input_chars = _length(input_value)
    result_chars = _length(result_value)
    sizes = []
    if input_chars is not None:
        sizes.append(f"输入 {input_chars} 字符")
    if result_chars is not None:
        sizes.append(f"结果 {result_chars} 字符")
    return f"调用工具（{'，'.join(sizes)}）" if sizes else "调用工具"


def build_tool_details(
    name: str,
    input_value: object,
    result_value: object,
    mode: str,
) -> dict[str, object]:
    if mode == "none":
        return {}
    if mode not in {"summary", "full"}:
        raise ValueError(f"unsupported tool detail mode: {mode}")
    output: dict[str, object] = {
        "detail_summary": summarize_tool_call(name, input_value, result_value)
    }
    if mode == "full":
        if input_value is not None:
            output["detail_input"] = sanitize_tool_value(
                parse_recorded_value(input_value)
            )
        if result_value is not None:
            output["detail_result"] = sanitize_tool_value(
                parse_recorded_value(result_value)
            )
    return output


def render_tool_value(value: object, *, limit: int = DEFAULT_TERMINAL_LIMIT) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        return text[:limit] + f"\n{TRUNCATED}"
    return text
