from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

from platform_supervisor.asset_contracts import (
    CapabilityErrorEvent,
    CapabilityErrorEventData,
)


_CAPABILITY_ERRORS = {
    "KNOWLEDGE_BASE_NO_PERMISSION": (
        "knowledge_base",
        "资产访问权限不足",
        True,
    ),
    "MCP_USER_AUTH_REQUIRED": ("mcp", "MCP 需要当前用户授权", True),
    "CAPABILITY_FORCE_DISABLED": ("skill", "该能力已被管理员停用", False),
    "CAPABILITY_PROJECTION_UNAVAILABLE": (
        "skill",
        "能力暂时不可用，系统正在重试",
        True,
    ),
}


def capability_error_from_tool_payload(
    payload: Mapping[str, Any],
    *,
    run_id: str,
) -> CapabilityErrorEvent | None:
    """Return a safe public capability event from an ordinary tool failure."""

    code = _find_error_code(payload)
    if code is None:
        return None
    capability_type, display_message, recoverable = _CAPABILITY_ERRORS[code]
    call_id = str(
        payload.get("tool_id")
        or payload.get("toolCallId")
        or payload.get("tool_call_id")
        or payload.get("call_id")
        or "unstable"
    )
    error_id = "caperr-" + sha256(
        f"{run_id}:{call_id}:{code}".encode("utf-8")
    ).hexdigest()[:24]
    return CapabilityErrorEvent(
        data=CapabilityErrorEventData(
            run_id=run_id,
            capability_type=capability_type,
            error_code=code,
            display_message=display_message,
            recoverable=recoverable,
            error_id=error_id,
        )
    )


def _find_error_code(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized in _CAPABILITY_ERRORS:
            return normalized
        if normalized.startswith("{") and normalized.endswith("}"):
            try:
                return _find_error_code(json.loads(normalized))
            except (TypeError, ValueError):
                return None
        return next(
            (code for code in _CAPABILITY_ERRORS if code in normalized),
            None,
        )
    if isinstance(value, Mapping):
        for key in ("error_code", "code", "detail", "error", "result"):
            if key in value:
                code = _find_error_code(value[key])
                if code is not None:
                    return code
    if isinstance(value, list):
        for item in value:
            code = _find_error_code(item)
            if code is not None:
                return code
    return None


__all__ = ["capability_error_from_tool_payload"]
