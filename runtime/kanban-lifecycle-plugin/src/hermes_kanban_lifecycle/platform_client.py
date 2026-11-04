from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PlatformClientError(RuntimeError):
    pass


def current_runtime_agent_profile_id() -> int | None:
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
    return profile_id if profile_id > 0 else None


class PlatformClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        workspace_id: int | None = None,
        token: str | None = None,
        timeout: float = 15.0,
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
        self.timeout = timeout

    def emit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url or not self.workspace_id or not self.token:
            raise PlatformClientError("Runtime Kanban platform settings are incomplete")
        body = dict(payload)
        profile_id = current_runtime_agent_profile_id()
        if profile_id is not None:
            body["runtime_agent_profile_id"] = profile_id
        request = Request(
            f"{self.base_url}/runtime-kanban/{self.workspace_id}/events",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise PlatformClientError("Runtime Kanban lease request failed") from exc
