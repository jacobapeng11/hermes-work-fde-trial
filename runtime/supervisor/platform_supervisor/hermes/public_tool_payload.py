from __future__ import annotations

import ipaddress
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit


MAX_PUBLIC_TOOL_TEXT = 12_000
MAX_PUBLIC_TOOL_DEPTH = 6
REDACTED = "••••"
DEPTH_LIMIT = "[内容层级过深]"
TRUNCATED = "[内容已截断]"
_FAILED_TOOL_STATUSES = {
    "failed",
    "failure",
    "error",
    "errored",
    "timeout",
    "timed_out",
    "canceled",
    "cancelled",
    "interrupted",
}
_RUNNING_TOOL_STATUSES = {"running", "pending", "queued", "in_progress"}
_COMPLETED_TOOL_STATUSES = {"completed", "complete", "success", "succeeded", "done"}

_SENSITIVE_KEY_PARTS = (
    "token",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "credential",
    "assertion",
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{2,}\b")
_API_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s\"'<>]+\\)*[^\\\s\"'<>]+")
_UNIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_>:/])/(?:[^/\s\"'<>]+/)+[^/\s\"'<>]+")


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_value(value: object, *, depth: int) -> object:
    if isinstance(value, dict):
        if depth >= MAX_PUBLIC_TOOL_DEPTH:
            return DEPTH_LIMIT
        return {
            str(key): REDACTED if _sensitive_key(key) else _sanitize_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if depth >= MAX_PUBLIC_TOOL_DEPTH:
            return DEPTH_LIMIT
        return [_sanitize_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _internal_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".svc")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:!?)]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname or ""
        if _internal_hostname(hostname):
            base = "<endpoint>"
        else:
            port = parts.port
            base = f"{parts.scheme}://{hostname}"
            if port:
                base += f":{port}"
        query = "&".join(
            f"{quote_plus(key)}={REDACTED if _sensitive_key(key) else quote_plus(value)}"
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        )
        return f"{base}{parts.path}{'?' + query if query else ''}" + trailing
    except ValueError:
        return "<endpoint>" + trailing


def _redact_windows_path(match: re.Match[str]) -> str:
    name = PureWindowsPath(match.group(0)).name
    return f"<path>/{_clean_filename(name)}"


def _redact_unix_path(match: re.Match[str]) -> str:
    name = PurePosixPath(match.group(0)).name
    return f"<path>/{_clean_filename(name)}"


def _clean_filename(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()
    return cleaned or "文件"


def _redact_text(value: str) -> str:
    text = _BEARER_RE.sub(REDACTED, value)
    text = _JWT_RE.sub(REDACTED, text)
    text = _API_KEY_RE.sub(REDACTED, text)
    text = _URL_RE.sub(_redact_url, text)
    text = _WINDOWS_PATH_RE.sub(_redact_windows_path, text)
    text = _UNIX_PATH_RE.sub(_redact_unix_path, text)
    return text


def sanitize_public_tool_text(value: object) -> str | None:
    if value is None:
        return None
    sanitized = _sanitize_value(value, depth=0)
    text = sanitized if isinstance(sanitized, str) else json.dumps(
        sanitized,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    text = _redact_text(text)
    if len(text) > MAX_PUBLIC_TOOL_TEXT:
        text = text[:MAX_PUBLIC_TOOL_TEXT] + f"\n{TRUNCATED}"
    return text or None


def build_public_tool_payload(
    *,
    name: str,
    status: str,
    call_id: str,
    has_stable_call_id: bool,
    input: object = None,
    preview: object = None,
    result: object = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name or "tool",
        "status": _normalize_tool_status(status),
        "callId": call_id,
        "hasStableCallId": has_stable_call_id,
    }
    input_text = sanitize_public_tool_text(input)
    preview_text = sanitize_public_tool_text(preview)
    result_text = sanitize_public_tool_text(result)
    if input_text:
        payload["input"] = input_text
    if preview_text:
        payload["preview"] = preview_text
    if result_text:
        payload["result"] = result_text
    return payload


def _normalize_tool_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _FAILED_TOOL_STATUSES:
        return "failed"
    if normalized in _COMPLETED_TOOL_STATUSES:
        return "completed"
    if normalized in _RUNNING_TOOL_STATUSES:
        return "running"
    return "running"


def _has_tool_failure_marker(item: dict[str, Any], result: object) -> bool:
    failure_keys = (
        "error",
        "error_message",
        "errorMessage",
        "exception",
        "failure",
        "failure_reason",
        "failureReason",
    )
    if any(item.get(key) not in (None, "", False) for key in failure_keys):
        return True
    if isinstance(result, dict):
        return any(result.get(key) not in (None, "", False) for key in failure_keys)
    return False


def _first_text(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _structured_tool_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def sanitize_public_tool_history_message(item: dict[str, Any], index: int) -> dict[str, Any]:
    if str(item.get("role") or "") != "tool":
        return item
    name = _first_text(item, "name", "toolName", "tool_name", "tool") or "tool"
    provider_call_id = _first_text(item, "callId", "toolCallId", "tool_call_id", "call_id")
    has_stable_call_id = bool(item.get("hasStableCallId") or item.get("has_stable_call_id"))
    call_id = provider_call_id if has_stable_call_id and provider_call_id else f"history-{index}"
    result_value = _structured_tool_value(item.get("result", item.get("content")))
    raw_status = item.get("status")
    status_value = (
        "failed"
        if _has_tool_failure_marker(item, result_value)
        else "completed"
        if raw_status in (None, "")
        else _normalize_tool_status(raw_status)
    )
    payload = build_public_tool_payload(
        name=name,
        status=status_value,
        call_id=call_id,
        has_stable_call_id=has_stable_call_id,
        input=_structured_tool_value(item.get("input")),
        preview=_structured_tool_value(item.get("preview")),
        result=result_value,
    )
    return {"role": "tool", **payload}
