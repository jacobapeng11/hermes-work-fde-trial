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
    def __init__(self, *, timeout: float = 5.0) -> None:
        self.base_url = os.environ.get("HERMES_PLATFORM_API_BASE_URL", "").rstrip("/")
        self.workspace_id = int(os.environ.get("HERMES_RUNTIME_WORKSPACE_ID", "0") or 0)
        self.token = os.environ.get("API_SERVER_KEY", "")
        self.timeout = timeout

    def create_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url or not self.workspace_id or not self.token:
            raise PlatformClientError("Runtime clarify platform settings are incomplete")
        request = Request(
            f"{self.base_url}/runtime-clarify/{self.workspace_id}/requests",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise PlatformClientError("Runtime clarify request could not be persisted") from exc
        if not isinstance(value, dict) or not isinstance(value.get("request_id"), str):
            raise PlatformClientError("Runtime clarify platform returned an invalid receipt")
        return value

    def get_request(self, request_id: str) -> dict[str, Any]:
        if not self.base_url or not self.workspace_id or not self.token:
            raise PlatformClientError("Runtime clarify platform settings are incomplete")
        request = Request(
            f"{self.base_url}/runtime-clarify/{self.workspace_id}"
            f"/requests/{request_id}",
            method="GET",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise PlatformClientError("Runtime clarify status request failed") from exc
        if not isinstance(value, dict):
            raise PlatformClientError("Runtime clarify platform returned invalid status")
        return value
