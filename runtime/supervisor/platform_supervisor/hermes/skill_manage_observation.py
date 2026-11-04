"""Safe extraction for Dashboard ``skill_manage`` completion events.

The Dashboard payload can contain a complete Skill body.  This module is the
only place that looks at the raw payload for the inventory synchronizer and it
returns a deliberately small immutable envelope.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass(frozen=True)
class ObservedSkillManageCompletion:
    tool_call_id: str | None
    action: Literal["create", "update", "delete"]
    skill_name: str
    duration_ms: int | None
    observed_at: datetime
    description: str | None = None
    summary: str | None = None


def extract_skill_manage_completion(payload: dict[str, Any]) -> ObservedSkillManageCompletion | None:
    """Return a safe completion envelope only for an explicit success.

    Missing tool IDs intentionally remain ``None``.  The caller uses that as
    a durable final-reconcile signal instead of inventing an unsafe idempotency
    key.
    """
    if payload.get("name") != "skill_manage":
        return None
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    action = args.get("action")
    if action not in {"create", "update", "delete"}:
        return None
    skill_name = _valid_skill_name(args.get("name"))
    if skill_name is None:
        return None
    result = _result_object(payload.get("result"))
    if result is None or result.get("success") is not True:
        return None
    details = result.get("skill") if isinstance(result.get("skill"), dict) else result
    return ObservedSkillManageCompletion(
        tool_call_id=_stable_id(payload.get("tool_id") or payload.get("toolCallId")),
        action=action,
        skill_name=skill_name,
        description=_bounded_text(details.get("description"), 2_000),
        summary=_bounded_text(
            details.get("summary") or details.get("readme_excerpt"),
            4_000,
        ),
        duration_ms=_duration_ms(payload.get("duration_s")),
        observed_at=datetime.now(timezone.utc),
    )


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def public_skill_manage_args(value: object) -> object:
    """Keep public tool telemetry useful without exposing Skill content."""
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key in ("action", "name"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            result[key] = item.strip()
    return result or None


def public_skill_manage_result(value: object) -> object:
    parsed = _result_object(value)
    if parsed is None:
        return None
    # Do not retain generated content or a provider response.  A Boolean is
    # sufficient for the user-facing tool timeline.
    return {"success": parsed.get("success") is True}


def _result_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > 16_384:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _stable_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if 0 < len(normalized) <= 200 else None


def _valid_skill_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        return None
    if "\x00" in normalized or "/" in normalized or "\\" in normalized:
        return None
    return normalized


def _duration_ms(value: object) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    milliseconds = round(value * 1000)
    return milliseconds if 0 <= milliseconds <= 600_000 else None
