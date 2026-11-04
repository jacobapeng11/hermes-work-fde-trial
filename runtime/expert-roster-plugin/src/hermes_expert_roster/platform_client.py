from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class PlatformClientError(RuntimeError):
    pass


def current_runtime_agent_profile_id() -> int | None:
    """Resolve the Backend-owned identity from the active Hermes Profile."""
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
        self.token = token if token is not None else os.environ.get("API_SERVER_KEY", "")
        self.runtime_agent_profile_id = runtime_agent_profile_id
        self.timeout = timeout

    def list_experts(self) -> dict[str, Any]:
        if not self.base_url or not self.workspace_id or not self.token:
            raise PlatformClientError(
                "runtime expert roster platform settings are incomplete"
            )
        profile_id = (
            self.runtime_agent_profile_id
            if self.runtime_agent_profile_id is not None
            else current_runtime_agent_profile_id()
        )
        query = (
            "?"
            + urlencode({"runtime_agent_profile_id": int(profile_id)})
            if profile_id is not None
            else ""
        )
        request = Request(
            f"{self.base_url}/runtime-experts/{self.workspace_id}/roster{query}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise PlatformClientError(
                "runtime expert roster platform request failed"
            ) from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("experts"), list):
            raise PlatformClientError(
                "runtime expert roster platform response is invalid"
            )
        return parsed
