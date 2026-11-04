from __future__ import annotations

import json
import logging
import os
import hashlib
import threading
import time
from uuid import uuid4
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


knowledge_logger = logging.getLogger("hermes_agent.knowledge")


KNOWN_PLATFORM_ERROR_CODES = frozenset(
    {
        "sql_empty",
        "sql_too_long",
        "sql_parse_failed",
        "sql_single_select_required",
        "sql_requires_configured_table",
        "sql_union_not_supported",
        "sql_cte_not_supported",
        "sql_subquery_not_supported",
        "sql_permission_table_forbidden",
        "sql_table_not_configured",
        "sql_lookup_first_not_supported",
        "sql_multiple_protected_tables",
        "sql_metric_contract_violation",
        "knowledge_identity_missing",
        "knowledge_scope_not_allowed",
        "knowledge_service_not_configured",
        "knowledge_service_unavailable",
        "knowledge_request_rejected",
        "knowledge_request_invalid",
        "knowledge_request_forbidden",
        "enterprise_knowledge_disabled",
    }
)
_PREPARED_STATE_TTL_SECONDS = 30.0
_PREPARED_STATE_MAX_ENTRIES = 128
_SQL_QUERY_TIMEOUT_SECONDS = float(
    os.environ.get("HERMES_ENTERPRISE_SQL_QUERY_TIMEOUT_SECONDS", "80")
)
_prepared_state_cache: dict[
    tuple[str, int, int, int, str, str],
    tuple[float, dict[str, Any]],
] = {}
_prepared_state_lock = threading.Lock()


class PlatformClientError(RuntimeError):
    def __init__(
        self,
        message: str = "runtime knowledge platform request failed",
        *,
        code: str = "knowledge_request_rejected",
        retryable: bool = False,
        hint: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.hint = hint
        self.status_code = status_code
        self.request_id = request_id

    def as_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            error["hint"] = self.hint
        if self.request_id:
            error["request_id"] = self.request_id
        return error


def _safe_http_error(exc: HTTPError) -> PlatformClientError:
    try:
        raw = exc.read(64 * 1024)
        body = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        detail = body.get("detail") if isinstance(body, dict) else None
        error = detail if isinstance(detail, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or code not in KNOWN_PLATFORM_ERROR_CODES:
        return PlatformClientError(status_code=exc.code)
    message = error.get("message")
    retryable = error.get("retryable")
    hint = error.get("hint")
    request_id = error.get("request_id")
    return PlatformClientError(
        message[:500] if isinstance(message, str) else "企业知识请求被拒绝。",
        code=code,
        retryable=retryable if isinstance(retryable, bool) else False,
        hint=hint[:1_000] if isinstance(hint, str) else None,
        request_id=(str(request_id)[:128] if isinstance(request_id, (str, int)) else None),
        status_code=exc.code,
    )


def current_runtime_agent_profile_id() -> int | None:
    """Resolve the platform identity from the currently bound Hermes Profile."""
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        platform = config.get("platform")
        value = (
            platform.get("runtime_agent_profile_id")
            if isinstance(platform, dict)
            else None
        )
        profile_id = int(value)
    except (ImportError, TypeError, ValueError):
        profile_id = 0
    if profile_id > 0:
        return profile_id
    try:
        from hermes_cli.profiles import get_active_profile_name

        profile_name = get_active_profile_name()
    except ImportError:
        # Older/default Runtime images have no platform identity in config.
        # Omitting the query preserves the Backend's canonical default-Profile
        # compatibility path.
        profile_name = "default"
    if profile_name != "default":
        raise PlatformClientError(
            "runtime agent profile identity is missing from profile config"
        )
    return None


class PlatformClient:
    def __init__(self, *, base_url: str | None = None, workspace_id: int | None = None, token: str | None = None, runtime_agent_profile_id: int | None = None, application_agent_id: int | None = None, timeout: float = 10.0) -> None:
        self.base_url = (base_url or os.environ.get("HERMES_PLATFORM_API_BASE_URL", "")).rstrip("/")
        raw_workspace_id = workspace_id if workspace_id is not None else os.environ.get("HERMES_RUNTIME_WORKSPACE_ID", "")
        self.workspace_id = int(raw_workspace_id) if raw_workspace_id else 0
        raw_application_agent_id = (
            application_agent_id
            if application_agent_id is not None
            else os.environ.get("HERMES_APPLICATION_AGENT_ID", "")
        )
        self.application_agent_id = (
            int(raw_application_agent_id)
            if raw_application_agent_id
            else 0
        )
        self.token = token if token is not None else os.environ.get("API_SERVER_KEY", "")
        self.runtime_agent_profile_id = runtime_agent_profile_id
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, runtime_session_id: str | None = None, turn_id: str | None = None, tool_call_id: str | None = None, prepared_context: bool = False, timeout: float | None = None) -> dict[str, Any]:
        if not self.base_url or not (self.workspace_id or self.application_agent_id) or not self.token:
            raise PlatformClientError("runtime knowledge platform settings are incomplete")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        profile_id = None
        profile_query = ""
        if self.application_agent_id:
            route = (
                "/application-agent-runtime-knowledge/"
                f"{self.application_agent_id}"
            )
            if not runtime_session_id:
                raise PlatformClientError(
                    "application Agent runtime session identity is missing"
                )
        else:
            route = f"/runtime-knowledge/{self.workspace_id}"
            profile_id = (
                self.runtime_agent_profile_id
                if self.runtime_agent_profile_id is not None
                else current_runtime_agent_profile_id()
            )
            if profile_id is not None:
                separator = "&" if "?" in path else "?"
                profile_query = (
                    f"{separator}runtime_agent_profile_id={int(profile_id)}"
                )
        request_id = str(tool_call_id or turn_id or f"knowledge-{uuid4().hex[:16]}")[:128]
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Hermes-Knowledge-Request-Id": request_id,
        }
        if runtime_session_id:
            headers["X-Hermes-Runtime-Session-Id"] = runtime_session_id
        if turn_id:
            headers["X-Hermes-Turn-Id"] = turn_id
        if tool_call_id:
            headers["X-Hermes-Tool-Call-Id"] = tool_call_id
        if prepared_context:
            headers["X-Hermes-Prepared-Context"] = "1"
        request = Request(
            f"{self.base_url}{route}{path}{profile_query}",
            data=body,
            method=method,
            headers=headers,
        )
        started = time.monotonic()
        request_timeout = self.timeout if timeout is None else timeout
        try:
            with urlopen(request, timeout=request_timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                knowledge_logger.debug(
                    "knowledge request completed request_id=%s stage=runtime_to_backend method=%s path=%s status=%s elapsed_ms=%.1f",
                    request_id,
                    method,
                    path,
                    getattr(response, "status", None),
                    (time.monotonic() - started) * 1000,
                )
                return result
        except HTTPError as exc:
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=runtime_to_backend method=%s path=%s status=%s error_type=http_error elapsed_ms=%.1f",
                request_id,
                method,
                path,
                exc.code,
                (time.monotonic() - started) * 1000,
            )
            raise _safe_http_error(exc) from exc
        except (URLError, TimeoutError) as exc:
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=runtime_to_backend method=%s path=%s error_type=%s elapsed_ms=%.1f timeout_s=%.1f",
                request_id,
                method,
                path,
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
                request_timeout,
            )
            raise PlatformClientError(
                "企业知识服务当前不可用。",
                code="knowledge_service_unavailable",
                retryable=False,
                request_id=request_id,
            ) from exc
        except (UnicodeError, ValueError) as exc:
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=runtime_to_backend method=%s path=%s error_type=%s elapsed_ms=%.1f",
                request_id,
                method,
                path,
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            raise PlatformClientError(
                "企业知识平台返回了无法识别的响应。",
                code="knowledge_request_rejected",
                retryable=False,
            ) from exc

    def state(self, *, runtime_session_id: str | None = None, turn_id: str | None = None, tool_call_id: str | None = None) -> dict[str, Any]:
        return self._request(
            "GET",
            "/state",
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )

    def prepared_state(
        self,
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        """Read hook state from a short session cache.

        Tool handlers deliberately continue to call ``state`` so every real
        data access retains its live platform authorization check.
        """
        if not runtime_session_id:
            return self.state(
                runtime_session_id=runtime_session_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
        profile_id = (
            0
            if self.application_agent_id
            else int(
                self.runtime_agent_profile_id
                if self.runtime_agent_profile_id is not None
                else (current_runtime_agent_profile_id() or 0)
            )
        )
        key = (
            self.base_url,
            int(self.workspace_id),
            int(self.application_agent_id),
            profile_id,
            str(runtime_session_id),
            hashlib.sha256(self.token.encode("utf-8")).hexdigest(),
        )
        now = time.monotonic()
        with _prepared_state_lock:
            cached = _prepared_state_cache.get(key)
            if cached is not None and cached[0] > now:
                return dict(cached[1])
            _prepared_state_cache.pop(key, None)
        value = self._request(
            "GET",
            "/state",
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            prepared_context=True,
        )
        with _prepared_state_lock:
            if len(_prepared_state_cache) >= _PREPARED_STATE_MAX_ENTRIES:
                oldest_key = min(
                    _prepared_state_cache,
                    key=lambda candidate: _prepared_state_cache[candidate][0],
                )
                _prepared_state_cache.pop(oldest_key, None)
            _prepared_state_cache[key] = (
                now + _PREPARED_STATE_TTL_SECONDS,
                dict(value),
            )
        return value

    def post(self, path: str, payload: dict[str, Any], *, runtime_session_id: str | None = None, turn_id: str | None = None, tool_call_id: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            path,
            payload,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            timeout=timeout,
        )

    def sql_query(self, payload: dict[str, Any], *, runtime_session_id: str | None = None, turn_id: str | None = None, tool_call_id: str | None = None) -> dict[str, Any]:
        return self.post(
            "/sql/query",
            payload,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            timeout=_SQL_QUERY_TIMEOUT_SECONDS,
        )
