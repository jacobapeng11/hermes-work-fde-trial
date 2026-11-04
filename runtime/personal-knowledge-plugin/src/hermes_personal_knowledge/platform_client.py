from __future__ import annotations

import json
import os
import hashlib
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError

from .contracts import PersonalKnowledgeClientError
from urllib.request import Request, urlopen


class PlatformClientError(PersonalKnowledgeClientError):
    pass


_PREPARED_STATE_TTL_SECONDS = 30.0
_PREPARED_STATE_MAX_ENTRIES = 128
_prepared_state_cache: dict[
    tuple[str, int, int, int, str, str],
    tuple[float, dict[str, Any]],
] = {}
_prepared_state_lock = threading.Lock()


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
        profile_name = "default"
    if profile_name != "default":
        raise PlatformClientError(
            "runtime agent profile identity is missing from profile config"
        )
    return None


class PlatformClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        workspace_id: int | None = None,
        token: str | None = None,
        runtime_agent_profile_id: int | None = None,
        application_agent_id: int | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("HERMES_PLATFORM_API_BASE_URL", "")
        ).rstrip("/")
        raw_workspace_id = (
            workspace_id
            if workspace_id is not None
            else os.environ.get("HERMES_RUNTIME_WORKSPACE_ID", "")
        )
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
        self.token = (
            token if token is not None else os.environ.get("API_SERVER_KEY", "")
        )
        self.runtime_agent_profile_id = runtime_agent_profile_id
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
        prepared_context: bool = False,
    ) -> dict[str, Any]:
        if (
            not self.base_url
            or not (self.workspace_id or self.application_agent_id)
            or not self.token
        ):
            raise PlatformClientError(
                "runtime personal knowledge platform settings are incomplete"
            )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        if self.application_agent_id:
            if not runtime_session_id:
                raise PlatformClientError(
                    "application Agent runtime session identity is missing"
                )
            route = (
                "/application-agent-runtime-personal-knowledge/"
                f"{self.application_agent_id}"
            )
            profile_id = None
        else:
            route = f"/runtime-personal-knowledge/{self.workspace_id}"
            profile_id = (
                self.runtime_agent_profile_id
                if self.runtime_agent_profile_id is not None
                else current_runtime_agent_profile_id()
            )
        profile_query = ""
        if profile_id is not None:
            separator = "&" if "?" in path else "?"
            profile_query = (
                f"{separator}runtime_agent_profile_id={int(profile_id)}"
            )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
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
        try:
            with urlopen(request, timeout=self.timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise PlatformClientError(
                "runtime personal knowledge platform request failed"
            ) from exc
        if not isinstance(parsed, dict):
            raise PlatformClientError(
                "runtime personal knowledge platform response is invalid"
            )
        return parsed

    def state(
        self,
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
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
        """Read pre-LLM state from a short session cache only."""
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

    def search(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/search",
            payload,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )

    def wiki_search(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/wiki-search",
            payload,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )

    def hybrid_search(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/hybrid-search",
            payload,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )

    def wiki_read_page(
        self,
        payload: dict[str, Any],
        *,
        runtime_session_id: str | None = None,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/wiki-read-page",
            payload,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )
