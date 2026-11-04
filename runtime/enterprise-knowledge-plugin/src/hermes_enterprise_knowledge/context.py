from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import time
from typing import Any

from .platform_client import PlatformClient, PlatformClientError


timing_logger = logging.getLogger("hermes_agent.runtime_timing")

_CONTEXT_OPEN = "<enterprise-knowledge>"
_CONTEXT_CLOSE = "</enterprise-knowledge>"

CONTEXT_HEADER = """<enterprise-knowledge>
The enterprise knowledge capability is enabled for this Runtime. Follow the complete PRIMER workflow below. Use enterprise_knowledge_read, enterprise_sql_context, enterprise_sql_query, and enterprise_document_search for enterprise retrieval; never request or infer a user token or user_code.
You must execute these tools directly in the current agent and must not delegate work that requires them to a subagent without these tools.
You must not use terminal to read /knowledge as a substitute. If a required enterprise tool is unavailable, report that limitation directly.

"""

_CONTEXT_BODY_HEADER = CONTEXT_HEADER.removeprefix(f"{_CONTEXT_OPEN}\n")
_ENABLED_STATE_TEXT = (
    "This state supersedes every earlier enterprise-knowledge-state marker and\n"
    "remains effective until a later marker supersedes it."
)
_ENABLED_BLOCK_PREFIX = (
    f"{_CONTEXT_OPEN}\n"
    '<enterprise-knowledge-state schema="1" status="enabled"\n'
    '  primer_sha256="'
)
_ENABLED_BLOCK_AFTER_SHA = f'" />\n{_ENABLED_STATE_TEXT}\n{_CONTEXT_BODY_HEADER}'
_ENABLED_BLOCK_CLOSE = f"\n{_CONTEXT_CLOSE}"
_LOWER_HEX = frozenset("0123456789abcdef")

DISABLED_MARKER = """<enterprise-knowledge-state schema="1" status="disabled">
Enterprise knowledge is disabled from this marker until a later
enterprise-knowledge-state marker supersedes it. Ignore prior
enterprise-knowledge instructions and do not call enterprise_* tools.
</enterprise-knowledge-state>"""

UNAVAILABLE_MARKER = """<enterprise-knowledge-state schema="1" status="unavailable">
Enterprise knowledge is unavailable from this marker until a later
enterprise-knowledge-state marker supersedes it. Ignore prior
enterprise-knowledge instructions and do not call enterprise_* tools.
</enterprise-knowledge-state>"""

_DEFAULT_SPILL_ENABLED = True
_DEFAULT_SPILL_MAX_CHARS = 10_000


@dataclass(frozen=True)
class _HistoryState:
    status: str = "none"
    format: str = "none"
    primer_sha256: str | None = None


_NO_HISTORY_STATE = _HistoryState()


def enterprise_knowledge_root() -> Path:
    return Path(os.environ.get("ENTERPRISE_KNOWLEDGE_ROOT", "/knowledge/enterprise")).resolve()


def _primer_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _legacy_context(content: str) -> str:
    return f"{CONTEXT_HEADER}{content}\n{_CONTEXT_CLOSE}"


def _enabled_context(content: str, primer_sha256: str) -> str:
    return (
        f"{_ENABLED_BLOCK_PREFIX}{primer_sha256}{_ENABLED_BLOCK_AFTER_SHA}"
        f"{content}{_ENABLED_BLOCK_CLOSE}"
    )


def _parse_enabled_block_at(
    suffix: str,
    start: int,
) -> tuple[int, str] | None:
    if not suffix.startswith(_ENABLED_BLOCK_PREFIX, start):
        return None

    sha_start = start + len(_ENABLED_BLOCK_PREFIX)
    sha_end = sha_start + 64
    primer_sha256 = suffix[sha_start:sha_end]
    if (
        len(primer_sha256) != 64
        or any(character not in _LOWER_HEX for character in primer_sha256)
        or not suffix.startswith(_ENABLED_BLOCK_AFTER_SHA, sha_end)
    ):
        return None

    primer_start = sha_end + len(_ENABLED_BLOCK_AFTER_SHA)
    close_at = suffix.find(_ENABLED_BLOCK_CLOSE, primer_start)
    while close_at >= 0:
        content = suffix[primer_start:close_at]
        if _primer_sha256(content) == primer_sha256:
            return close_at + len(_ENABLED_BLOCK_CLOSE), primer_sha256
        close_at = suffix.find(
            _ENABLED_BLOCK_CLOSE,
            close_at + len(_ENABLED_BLOCK_CLOSE),
        )
    return None


def _latest_state_in_suffix(
    suffix: str,
    *,
    legacy_context: str | None = None,
    legacy_sha256: str | None = None,
) -> _HistoryState:
    latest_position = -1
    latest_state = _NO_HISTORY_STATE

    start = suffix.find(_ENABLED_BLOCK_PREFIX)
    while start >= 0:
        parsed = _parse_enabled_block_at(suffix, start)
        if parsed is not None and start > latest_position:
            _, primer_sha256 = parsed
            latest_position = start
            latest_state = _HistoryState(
                status="enabled",
                format="marker",
                primer_sha256=primer_sha256,
            )
        start = suffix.find(_ENABLED_BLOCK_PREFIX, start + 1)

    for status, marker in (
        ("disabled", DISABLED_MARKER),
        ("unavailable", UNAVAILABLE_MARKER),
    ):
        position = suffix.rfind(marker)
        if position > latest_position:
            latest_position = position
            latest_state = _HistoryState(status=status, format="marker")

    if legacy_context is not None and legacy_sha256 is not None:
        position = suffix.rfind(legacy_context)
        if position > latest_position:
            latest_state = _HistoryState(
                status="enabled",
                format="legacy",
                primer_sha256=legacy_sha256,
            )

    return latest_state


def _history_state(
    conversation_history: object,
    *,
    legacy_context: str | None = None,
    legacy_sha256: str | None = None,
) -> _HistoryState:
    if (
        not isinstance(conversation_history, Sequence)
        or isinstance(conversation_history, (str, bytes, bytearray))
    ):
        return _NO_HISTORY_STATE

    for message in reversed(conversation_history):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        api_content = message.get("api_content")
        if not isinstance(content, str) or not isinstance(api_content, str):
            continue
        prefix = f"{content}\n\n"
        if not api_content.startswith(prefix):
            continue
        suffix = api_content[len(prefix):]
        state = _latest_state_in_suffix(
            suffix,
            legacy_context=legacy_context,
            legacy_sha256=legacy_sha256,
        )
        if state.status != "none":
            return state
    return _NO_HISTORY_STATE


def _hook_output_spill_config() -> Mapping[str, Any]:
    try:
        from tools.hook_output_spill import get_spill_config

        config = get_spill_config()
        if isinstance(config, Mapping):
            return config
    except Exception:
        # Hermes uses the same defaults if its config cannot be loaded.
        pass
    return {
        "enabled": _DEFAULT_SPILL_ENABLED,
        "max_chars": _DEFAULT_SPILL_MAX_CHARS,
    }


def _would_spill(context: str) -> bool:
    config = _hook_output_spill_config()
    if not config.get("enabled", _DEFAULT_SPILL_ENABLED):
        return False
    try:
        max_chars = int(
            config.get("max_chars") or _DEFAULT_SPILL_MAX_CHARS
        )
    except (TypeError, ValueError):
        max_chars = _DEFAULT_SPILL_MAX_CHARS
    if max_chars <= 0:
        max_chars = _DEFAULT_SPILL_MAX_CHARS
    return len(context) > max_chars


def _duration_ms(started_at: float, finished_at: float) -> float:
    return round(max(0.0, finished_at - started_at) * 1000, 3)


def _safe_correlation_id(value: object) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    candidate = str(value)
    if not candidate or len(candidate) > 128:
        return None
    if not all(
        character.isascii()
        and (character.isalnum() or character in "._:-")
        for character in candidate
    ):
        return None
    return candidate


def _log_hook_timing(
    *,
    hook_context: dict,
    stage: str,
    status: str,
    started_at: float,
    finished_at: float,
    state_finished_at: float | None = None,
    primer_finished_at: float | None = None,
    error_type: str | None = None,
    context_action: str,
    history_state: _HistoryState,
    primer_sha256: str | None = None,
) -> None:
    durations_ms = {
        "total": _duration_ms(started_at, finished_at),
    }
    if state_finished_at is not None:
        durations_ms["state_http"] = _duration_ms(
            started_at,
            state_finished_at,
        )
    if state_finished_at is not None and primer_finished_at is not None:
        durations_ms["primer_read"] = _duration_ms(
            state_finished_at,
            primer_finished_at,
        )
        if stage == "context_build_done":
            durations_ms["context_build"] = _duration_ms(
                primer_finished_at,
                finished_at,
            )
    elif state_finished_at is not None and stage == "primer_read":
        durations_ms["primer_read"] = _duration_ms(
            state_finished_at,
            finished_at,
        )
    duration_ms = durations_ms.get(
        "context_build",
        durations_ms.get(
            "primer_read",
            durations_ms.get("state_http", durations_ms["total"]),
        ),
    )
    payload = {
        "component": "enterprise_knowledge",
        "channel": "runtime",
        "stage": stage,
        "duration_ms": duration_ms,
        "durations_ms": durations_ms,
        "status": status,
        "context_action": context_action,
        "history_state": history_state.status,
        "history_format": history_state.format,
        "runtime_session_id": _safe_correlation_id(
            hook_context.get("session_id")
        ),
        "turn_id": _safe_correlation_id(hook_context.get("turn_id")),
    }
    if primer_sha256 is not None:
        payload["primer_sha256_prefix"] = primer_sha256[:12]
    if error_type:
        payload["error_type"] = error_type
    try:
        timing_logger.info("agent ttft timing=%s", payload)
    except Exception:
        # Observability must never change hook behavior.
        pass


def build_context(
    client: PlatformClient | None = None,
    root: Path | None = None,
    **hook_context,
):
    started_at = time.perf_counter()
    conversation_history = hook_context.get("conversation_history")
    history_state = _history_state(conversation_history)
    client = client or PlatformClient()
    try:
        state_loader = getattr(client, "prepared_state", client.state)
        state = state_loader(
            runtime_session_id=hook_context.get("session_id"),
            turn_id=hook_context.get("turn_id"),
            tool_call_id=hook_context.get("tool_call_id"),
        )
    except PlatformClientError as exc:
        finished_at = time.perf_counter()
        _log_hook_timing(
            hook_context=hook_context,
            stage="state_http",
            status="error",
            started_at=started_at,
            finished_at=finished_at,
            error_type=type(exc).__name__,
            context_action=(
                "deduplicated"
                if history_state.status == "unavailable"
                else "unavailable_marker"
            ),
            history_state=history_state,
        )
        if history_state.status == "unavailable":
            return None
        return {"context": UNAVAILABLE_MARKER}
    state_finished_at = time.perf_counter()
    if state.get("enabled") is not True:
        context_action = (
            "deduplicated"
            if history_state.status == "disabled"
            else "disabled_marker"
        )
        _log_hook_timing(
            hook_context=hook_context,
            stage="state_http_done",
            status="disabled",
            started_at=started_at,
            finished_at=state_finished_at,
            state_finished_at=state_finished_at,
            context_action=context_action,
            history_state=history_state,
        )
        if history_state.status == "disabled":
            return None
        return {"context": DISABLED_MARKER}
    primer = (root or enterprise_knowledge_root()) / "PRIMER.md"
    try:
        content = primer.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        finished_at = time.perf_counter()
        _log_hook_timing(
            hook_context=hook_context,
            stage="primer_read",
            status="error",
            started_at=started_at,
            finished_at=finished_at,
            state_finished_at=state_finished_at,
            error_type=type(exc).__name__,
            context_action=(
                "deduplicated"
                if history_state.status == "unavailable"
                else "unavailable_marker"
            ),
            history_state=history_state,
        )
        if history_state.status == "unavailable":
            return None
        return {"context": UNAVAILABLE_MARKER}
    primer_finished_at = time.perf_counter()
    primer_sha256 = _primer_sha256(content)
    history_state = _history_state(
        conversation_history,
        legacy_context=_legacy_context(content),
        legacy_sha256=primer_sha256,
    )
    context = _enabled_context(content, primer_sha256)
    if (
        history_state.status == "enabled"
        and history_state.primer_sha256 == primer_sha256
    ):
        result = None
        context_action = "deduplicated"
    elif _would_spill(context):
        context_action = "spill_blocked"
        result = (
            None
            if history_state.status == "unavailable"
            else {"context": UNAVAILABLE_MARKER}
        )
    else:
        result = {"context": context}
        context_action = "primer_injected"
    finished_at = time.perf_counter()
    _log_hook_timing(
        hook_context=hook_context,
        stage="context_build_done",
        status="enabled",
        started_at=started_at,
        finished_at=finished_at,
        state_finished_at=state_finished_at,
        primer_finished_at=primer_finished_at,
        context_action=context_action,
        history_state=history_state,
        primer_sha256=primer_sha256,
    )
    return result
